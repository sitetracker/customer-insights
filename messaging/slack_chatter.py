import requests

from slack_sdk import WebClient


class SlackChatter:
    def __init__(self, slack_client, slack_channel, ts=None, response_url=None):
        self.slack_client = slack_client
        self.slack_channel = slack_channel
        self.ts = ts
        self.response_url = response_url

    def emit_message(self, text, blocks=None, ephemeral_user=None, update_ts=None):
        if update_ts:
            return self.slack_client.chat_update(
                channel=self.slack_channel, ts=update_ts, text=text, blocks=blocks
            )
        elif ephemeral_user:
            return self.slack_client.chat_postEphemeral(
                channel=self.slack_channel,
                user=ephemeral_user,
                text=text,
                blocks=blocks,
            )
        elif self.response_url:
            return requests.post(
                self.response_url,
                json={
                    "text": text,
                    "replace_original": True,
                    "response_type": "ephemeral",
                    "blocks": blocks,
                },
            )
        return self.slack_client.chat_postMessage(
            channel=self.slack_channel, text=text, blocks=blocks
        )
