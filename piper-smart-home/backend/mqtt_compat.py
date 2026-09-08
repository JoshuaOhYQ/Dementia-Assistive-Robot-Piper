"""Small shim so the same code runs on paho-mqtt 1.x and 2.x.

paho-mqtt 2.0 changed the constructor: it now requires a callback API version.
Asking for VERSION1 keeps the familiar on_connect(client, userdata, flags, rc)
signatures, so one codebase works with whatever pip installs.
"""
import paho.mqtt.client as mqtt


def make_client(client_id: str, clean_session: bool = True) -> mqtt.Client:
    try:
        return mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,      # paho 2.x
            client_id=client_id,
            clean_session=clean_session,
        )
    except AttributeError:
        return mqtt.Client(client_id=client_id, clean_session=clean_session)  # paho 1.x
