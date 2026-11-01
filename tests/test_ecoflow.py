import json

import httpx
import pytest

from ecoflow_monitor.adapters.ecoflow import Credentials, EcoFlowClient, EcoFlowError, flatten_parameters, normalize_telemetry, signed_headers
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
