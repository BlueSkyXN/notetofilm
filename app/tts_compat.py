"""Compatibility for edge-tts 7.2.8's retired synthesis endpoint.

The voice-list endpoint remains unchanged. Remove this shim after upgrading to
a release incorporating https://github.com/rany2/edge-tts/pull/491.
"""

from urllib.parse import urlencode, urlsplit

import edge_tts
from edge_tts import communicate, constants


def configure_synthesis_endpoint():
    # Initialize once before workers start; never switch globals per request.
    if edge_tts.__version__ == '7.2.8' and urlsplit(communicate.WSS_URL).hostname == 'speech.platform.bing.com':
        communicate.WSS_URL = ('wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1?'
                               + urlencode({'Ocp-Apim-Subscription-Key': constants.TRUSTED_CLIENT_TOKEN}))
