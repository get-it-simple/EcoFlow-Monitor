import json
import ssl
from types import SimpleNamespace

import httpx
import pytest
from paho.mqtt import client as mqtt

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError, flatten_parameters, normalize_telemetry, signed_headers
from ecoflow_monitor.adapters.ecoflow_mqtt import BrokerAccess, MqttCancellation, MqttError, decode_report, stream_telemetry
from ecoflow_monitor.domain import ConnectionState


def test_official_general_information_signature_vector():
    credentials = Credentials("Fp4SvIprYSDPXtYJidEtUAd1o", "WIbFEKre0s6sLnh4ei7SPUeYnptHG6V")
    parameters = {"sn": "123456789", "params": {"cmdSet": 11, "id": 24, "eps": 0}}
    headers = signed_headers(parameters, credentials, 345164, 1671171709428)
    assert headers["sign"] == "07c13b65e037faf3b153d51613638fa80003c4c38d2407379a7f52851af1473e"
    assert "Fp4" not in repr(credentials)
    assert "WIbF" not in repr(credentials)


def test_documented_nested_parameter_flattening():
    pairs = flatten_parameters({"name": "demo1", "ids": [1, 2, 3], "deviceInfo": {"id": 1}, "deviceList": [{"id": 1}, {"id": 2}]})
    assert "&".join(f"{key}={value}" for key, value in sorted(pairs)) == "deviceInfo.id=1&deviceList[0].id=1&deviceList[1].id=2&ids[0]=1&ids[1]=2&ids[2]=3&name=demo1"


def test_official_delta3max_response_and_read_only_requests():
    requests = []
    def handle(request):
        requests.append(request)
        assert request.url.host == "api.ecoflow.com"
        assert len(request.headers["nonce"]) == 6
        assert len(request.headers["sign"]) == 64
        if request.url.path.endswith("/list"):
            return httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST-SERIAL", "deviceName": "My Max", "online": 1}]})
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json;charset=UTF-8"
        body = json.loads(request.content)
        assert set(body) == {"sn", "params"}
        assert set(body["params"]) == {"quotas"}
        return httpx.Response(200, json={"code": "0", "data": {"powInSumW": 0, "powOutSumW": "280", "cmsBattSoc": 80, "cmsMaxChgSoc": 90, "cmsMinDsgSoc": 20, "powGetAcOut": 280, "cmsDsgRemTime": 437, "powGet12vList.powGet12vItem": [0, 3]}})
    snapshot = EcoFlowClient(Credentials("test-access", "test-secret"), httpx.MockTransport(handle)).read("TEST-SERIAL")
    assert snapshot.input_w == 0 and snapshot.output_w == 280 and snapshot.soc_pct == 80
    assert snapshot.min_soc == 20 and snapshot.max_soc == 90
    assert snapshot.device_discharge_minutes == 437
    assert dict(snapshot.ports)["anderson_2"] == 3
    assert len(requests) == 2 and all(request.method != "PUT" for request in requests)


@pytest.mark.parametrize("value", [None, True, "NaN", "inf", -1, {}, []])
def test_bad_required_value_never_becomes_a_zero(value):
    with pytest.raises(EcoFlowError) as error:
        normalize_telemetry({"powInSumW": value, "powOutSumW": 0, "cmsBattSoc": 50}, 1, 2)
    assert error.value.state == ConnectionState.API_ERROR


def test_missing_model_fields_are_unsupported_and_optional_fields_stay_unknown():
    with pytest.raises(EcoFlowError) as error:
        normalize_telemetry({"pd.soc": 50}, 1, 2)
    assert error.value.state == ConnectionState.UNSUPPORTED
    snapshot = normalize_telemetry({"powInSumW": 0, "powOutSumW": 0, "cmsBattSoc": 0}, 1, 2)
    assert snapshot.soc_pct == 0 and snapshot.min_soc is None and snapshot.ports == ()


@pytest.mark.parametrize("status,state", [(401, ConnectionState.AUTH_ERROR), (403, ConnectionState.AUTH_ERROR), (429, ConnectionState.API_ERROR), (500, ConnectionState.API_ERROR)])
def test_http_failures_are_sanitized(status, state):
    client = EcoFlowClient(Credentials("private-access", "private-secret"), httpx.MockTransport(lambda request: httpx.Response(status, headers={"Retry-After": "60"})))
    with pytest.raises(EcoFlowError) as error:
        client.devices()
    assert error.value.state == state
    assert "private" not in str(error.value)


def test_offline_device_is_not_read_or_used_as_zero_input():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": "0", "data": [{"sn": "TEST", "online": 0}]}))
    with pytest.raises(EcoFlowError) as error:
        EcoFlowClient(Credentials("test", "test"), transport).read("TEST")
    assert error.value.state == ConnectionState.DEVICE_OFFLINE


def test_mqtt_report_preserves_partial_values_and_decodes_nested_anderson_outputs():
    patch = decode_report(b'{"powInSumW":0,"cmsBattSoc":null,"powGet12vList":{"powGet12vItem":[0,3]}}')
    assert dict(patch.values) == {"input_power": 0, "anderson": (0, 3)}
    assert set(patch.requested) == {"input_power", "battery_level", "anderson"}
    assert patch.invalid == ("battery_level",)
    assert not decode_report(b'{"unknown":0}').requested
    assert not decode_report(b'[').requested


def test_signed_mqtt_certification_never_logs_broker_credentials(tmp_path):
    from ecoflow_monitor.diagnostics import DiagnosticLog

    account, password = "synthetic-broker-account", "synthetic-broker-password"
    data = {"url": "mqtt.ecoflow.com", "port": "8883", "protocol": "mqtts",
            "certificateAccount": account, "certificatePassword": password}

    def handle(request):
        assert request.method == "GET" and request.url.path == "/iot-open/sign/certification"
        assert len(request.headers["sign"]) == 64
        return httpx.Response(200, json={"code": "0", "data": data, "echo": account})

    path = tmp_path / "connection.log"
    with DiagnosticLog(path) as log:
        log.configure("detailed")
        client = EcoFlowClient(Credentials("test-access", "test-secret"), httpx.MockTransport(handle))
        access = BrokerAccess.from_response(client.mqtt_certification())
        assert access.port == 8883 and access.account == account
        assert account not in repr(access) and password not in repr(access)
    output = path.read_text(encoding="utf-8")
    assert "/iot-open/sign/certification" in output
    assert account not in output and password not in output

    for change in ({"protocol": "mqtt"}, {"url": "mqtt.ecoflow.com.invalid"}, {"certificateAccount": "+"}):
        with pytest.raises(MqttError):
            BrokerAccess.from_response(data | change)


def test_mqtt_stream_subscribes_to_only_the_live_device_topic_with_verified_tls(monkeypatch):
    topic = "/open/test-account/TEST/quota"
    cancelled = MqttCancellation()
    subscribed = []
    received = []
    disconnected = []

    class Client(mqtt.Client):
        def __init__(self, version, *, protocol, reconnect_on_failure):
            assert version is mqtt.CallbackAPIVersion.VERSION2 and protocol == mqtt.MQTTv311 and not reconnect_on_failure
            super().__init__(version, protocol=protocol, reconnect_on_failure=reconnect_on_failure)

        def tls_set_context(self, context):
            assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
            super().tls_set_context(context)

        def username_pw_set(self, account, password):
            assert (account, password) == ("test-account", "test-password")
            super().username_pw_set(account, password)

        def connect(self, host, port, keepalive):
            assert (host, port, keepalive) == ("mqtt.ecoflow.com", 8883, 30)
            return mqtt.MQTT_ERR_SUCCESS

        def subscribe(self, selected, qos):
            subscribed.append(selected)
            assert qos == 0
            return mqtt.MQTT_ERR_SUCCESS, 1

        def socket(self):
            return self

        def loop_read(self):
            self.on_connect(self, None, None, mqtt.ReasonCode(mqtt.PacketTypes.CONNACK), None)
            self.on_subscribe(self, None, 1, [mqtt.ReasonCode(mqtt.PacketTypes.SUBACK, identifier=0)], None)
            for selected, retain in ((topic + "/other", False), (topic, True), (topic, False)):
                report = SimpleNamespace(topic=selected, retain=retain, payload=b'{"cmsBattSoc":70}')
                self.on_message(self, None, report)
            cancelled.set()
            return mqtt.MQTT_ERR_SUCCESS

        def loop_misc(self):
            return mqtt.MQTT_ERR_SUCCESS

        def disconnect(self):
            disconnected.append(True)

    monkeypatch.setattr(mqtt, "Client", Client)
    monkeypatch.setattr("ecoflow_monitor.adapters.ecoflow_mqtt.select", lambda read, write, errors, timeout: (read[:1], [], []))
    access = BrokerAccess("mqtt.ecoflow.com", 8883, "test-account", "test-password")
    stream_telemetry(access, "TEST", cancelled, received.append)
    assert subscribed == [topic] and disconnected == [True]
    assert [dict(patch.values) for patch in received] == [{"battery_level": 70}]
