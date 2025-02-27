import re
from typing import Optional
from posthog.cdp.filters import compile_filters_bytecode
from posthog.cdp.validation import compile_hog, validate_inputs
from posthog.models.action.action import Action
from posthog.models.hog_functions.hog_function import HogFunction
from posthog.cdp.templates.webhook.template_webhook import template as webhook_template
from posthog.plugins.plugin_server_api import reload_all_hog_functions_on_workers

# Maps to a string or a tuple of name and url
mappings: dict[str, str | list[str]] = {
    "[event]": ["{event.event}", "{event.url}"],
    "[event.link]": "{event.url}",
    "[event.event]": "{event.event}",
    "[event.name]": "{event.event}",
    "[event.uuid]": "{event.uuid}",
    "[person]": ["{person.name}", "{person.url}"],
    "[person.link]": "{person.url}",
    "[person.uuid]": "{person.id}",
    "[user]": ["{person.name}", "{person.url}"],
    "[user.name]": "{person.name}",
    "[user.pathname]": "{event.properties.$pathname}",
    "[user.email]": "{person.properties.email}",
    "[user.distinct_id]": "{person.distinct_id}",
    "[user.host]": "{event.properties.$host}",
    "[user.os]": "{event.properties.$os}",
    "[user.initial_referrer]": "{event.properties.$initial_referrer}",
    "[user.time]": "{event.timestamp}",
}

inert_fetch_print = """
print('Mocked webhook', inputs.url, {
  'headers': inputs.headers,
  'body': inputs.body,
  'method': inputs.method
})
""".strip()


def convert_link(text: str, url: str, is_slack: bool) -> str:
    if is_slack:
        return f"<{url}|{text}>"
    return f"[{text}]({url})"


def convert_slack_message_format_to_hog(action: Action, is_slack: bool) -> tuple[str, str]:
    message_format = action.slack_message_format or "[action.name] triggered by [person]"
    message_format = message_format.replace("{", "\\{")
    matches = re.findall(r"(\[[^\]]+\])", message_format)
    markdown = message_format
    text = message_format
    # Iterate over each match replacing it with the appropriate hog format
    for match in matches:
        content = match[1:-1]
        if match in mappings:
            if isinstance(mappings[match], list):
                # For markdown we create a link
                markdown = markdown.replace(match, convert_link(mappings[match][0], mappings[match][1], is_slack))
                # For text we just replace it with the name
                text = text.replace(match, mappings[match][0])
            else:
                markdown = markdown.replace(match, str(mappings[match]))
                text = text.replace(match, str(mappings[match]))
        elif match.startswith("[action."):
            # Action data is no longer available as it is just a filter hence we need to replace it with static values
            action_property = content.split(".")[1]
            action_url = f"{{project.url}}/data-management/actions/{action.id}"
            if action_property == "link":
                text = text.replace(match, action_url)
                markdown = markdown.replace(match, action_url)
            else:
                markdown = markdown.replace(match, convert_link(action.name or "Action", action_url, is_slack))
                text = text.replace(match, action.name or "Action")
        elif match.startswith("[groups."):
            parts = content.split(".")
            if len(parts) == 2:
                # this means it is a link to the group - we just need to append "url"
                markdown = markdown.replace(match, f"{{{content}.url}}")
                text = text.replace(match, f"{{{content}.url}}")
            else:
                # Only other supported thing is properties which happens to match the format
                markdown = markdown.replace(match, f"{{{content}}}")
                text = text.replace(match, f"{{{content}}}")
        elif match.startswith("[user."):
            parts = content.split(".")
            string = ".".join(["person", "properties", "$" + parts[1], *parts[2:]])
            markdown = markdown.replace(match, f"{{{string}}}")
            text = text.replace(match, f"{{{string}}}")
        elif match.startswith("[properties."):
            parts = content.split(".")
            string = ".".join(["event", *parts])
            markdown = markdown.replace(match, f"{{{string}}}")
            text = text.replace(match, f"{{{string}}}")
        else:
            markdown = markdown.replace(match, f"{{{content}}}")
            text = text.replace(match, f"{{{content}}}")
    print(  # noqa: T201
        f"[Action {action.id}] Converted message format:",
        {
            "original": message_format,
            "markdown": markdown,
            "text": text,
        },
    )
    return (markdown, text)


def convert_to_hog_function(action: Action, inert=False) -> Optional[HogFunction]:
    webhook_url = action.team.slack_incoming_webhook
    if not webhook_url:
        return None
    message_markdown, message_text = convert_slack_message_format_to_hog(action, is_slack="slack" in webhook_url)
    if "slack" in webhook_url:
        body = {
            "text": message_text,
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": message_markdown}}],
        }
    else:
        body = {
            "text": message_markdown,
        }
    hog_code = inert_fetch_print if inert else webhook_template.hog
    hog_name = f"Webhook for action {action.id} ({action.name})"
    if inert:
        hog_name = f"[CDP-TEST-HIDDEN] {hog_name}"
    hog_function = HogFunction(
        name=hog_name,
        type="destination",
        description="Automatically migrated from legacy action webhooks",
        team_id=action.team_id,
        inputs=validate_inputs(
            webhook_template.inputs_schema,
            {"url": {"value": webhook_url}, "method": {"value": "POST"}, "body": {"value": body}},
            function_type="destination",
        ),
        inputs_schema=webhook_template.inputs_schema,
        template_id=webhook_template.id,
        hog=hog_code,
        bytecode=compile_hog(hog_code, "destination"),
        filters=compile_filters_bytecode(
            {"actions": [{"id": f"{action.id}", "type": "actions", "name": action.name, "order": 0}]}, action.team
        ),
        enabled=True,
        icon_url=webhook_template.icon_url,
    )
    return hog_function


def migrate_all_teams_action_webhooks(dry_run=False, inert=False):
    """Migrate actions for all teams in the system."""
    print("Starting migration of actions for all teams")  # noqa: T201

    # Get the query without evaluating it
    query = Action.objects.select_related("team").filter(post_to_slack=True, deleted=False).order_by("id")

    batch_size = 100
    hog_functions: list[HogFunction] = []
    actions_to_update: list[Action] = []
    processed_count = 0

    # Use iterator() to fetch records in batches (instead of paginator)
    for action in query.iterator(chunk_size=batch_size):
        if len(action.steps) == 0:
            continue

        try:
            hog_function = convert_to_hog_function(action, inert)
            if hog_function:
                hog_functions.append(hog_function)
                if not inert:
                    action.post_to_slack = False
                    actions_to_update.append(action)
        except Exception as e:
            print(f"Failed to migrate action {action.id}: {e}")  # noqa: T201

        processed_count += 1

        # Bulk create/update when we reach batch_size
        if len(hog_functions) >= batch_size:
            if not dry_run:
                HogFunction.objects.bulk_create(hog_functions)
                if actions_to_update:
                    Action.objects.bulk_update(actions_to_update, ["post_to_slack"])
            else:
                print(f"Would have created {len(hog_functions)} HogFunctions")  # noqa: T201

            hog_functions = []
            actions_to_update = []

    # Handle any remaining items
    if hog_functions:
        if not dry_run:
            HogFunction.objects.bulk_create(hog_functions)
            if actions_to_update:
                Action.objects.bulk_update(actions_to_update, ["post_to_slack"])
        else:
            print(f"Would have created {len(hog_functions)} HogFunctions")  # noqa: T201

    print(f"Processed {processed_count} actions in total")  # noqa: T201

    if not dry_run:
        reload_all_hog_functions_on_workers()
