from slack_sdk import WebClient


class SlackChatter:
    def __init__(self, slack_client, slack_channel, ts=None, update=False):
        self.slack_client = slack_client
        self.slack_channel = slack_channel
        self.ts = ts
        self.update = update

    def emit_message(self, text):
        if self.update and self.ts:
            return self.slack_client.chat_update(
                channel=self.slack_channel, ts=self.ts, text=text
            )
        return self.slack_client.chat_postMessage(channel=self.slack_channel, text=text)
