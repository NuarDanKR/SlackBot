import pytest

from tybot.master_v2_rollout import ENV_NAME, RolloutConfigError
from tybot.specialist_v3_endpoints import SHARED_ENV_NAME, EndpointPolicy


def test_shared_is_opt_in_and_default_preserves_tyit():
    policy = EndpointPolicy.from_env({ENV_NAME: "tyit"})
    endpoint = policy.for_request("hermes", "tyit")
    assert endpoint.socket == "/run/tybot-subbots/hermes-tyit/http.sock"
    assert endpoint.credential_workspace == "tyit"


def test_only_explicit_hermes_workspaces_share_the_transport_and_credential():
    policy = EndpointPolicy.from_env({
        ENV_NAME: "tyit,mgmt,pilot", SHARED_ENV_NAME: "tyit,mgmt",
    })
    for workspace in ("tyit", "mgmt"):
        endpoint = policy.for_request("hermes", workspace)
        assert endpoint.socket == "/run/tybot-subbots/hermes-shared/http.sock"
        assert endpoint.credential_workspace == "shared"
    assert policy.for_request("hermes", "pilot").credential_workspace == "pilot"
    assert policy.for_request("clio", "tyit").credential_workspace == "tyit"


@pytest.mark.parametrize("value", ["*", "tyit,*", "ty it", "tyit,", "mgmt"])
def test_invalid_shared_config_cannot_enable_an_unreviewed_workspace(value):
    with pytest.raises(RolloutConfigError):
        EndpointPolicy.from_env({ENV_NAME: "tyit", SHARED_ENV_NAME: value})


def test_shared_transport_does_not_bypass_request_key_validation():
    policy = EndpointPolicy(frozenset({"../tyit"}))
    with pytest.raises(ValueError):
        policy.for_request("hermes", "../tyit")


def test_invalid_shared_allowlist_blocks_start_even_when_master_is_off(monkeypatch):
    from tybot.slack import pilot

    monkeypatch.setenv(ENV_NAME, "")
    monkeypatch.setenv(SHARED_ENV_NAME, "tyit")
    with pytest.raises(RolloutConfigError):
        pilot._start_external_v3([])


def test_bridge_uses_shared_credentials_but_preserves_request_authority(monkeypatch):
    from types import SimpleNamespace

    from tybot.slack import pilot

    captured = []

    class FakeServer:
        def __init__(self, registry):
            pass

        def start(self):
            pass

        def register_credential(self, identity, secret):
            assert identity == "hermes:shared"
            assert secret == b"x" * 32

    class FakeCredentials:
        def __init__(self, directory):
            assert directory == "/credentials"

        def get(self, specialist, workspace):
            assert (specialist, workspace) == ("hermes", "shared")
            return "hermes:shared", b"x" * 32

    def adapter(key, **kwargs):
        captured.append((key, kwargs))
        return kwargs

    monkeypatch.setenv(ENV_NAME, "tyit,mgmt")
    monkeypatch.setenv(SHARED_ENV_NAME, "tyit,mgmt")
    monkeypatch.setenv("TYBOT_SPECIALIST_V3_HMAC_DIR", "/credentials")
    monkeypatch.setattr("tybot.specialist_v3_tool_server.ToolServer", FakeServer)
    monkeypatch.setattr("tybot.specialist_v3_secrets.CredentialStore", FakeCredentials)
    monkeypatch.setattr("tybot.specialist_v3_client.ExternalV3Adapter", adapter)
    monkeypatch.setattr(pilot, "_external_v3_tool_server", None)
    factory = pilot._start_external_v3([
        SimpleNamespace(key="tyit"), SimpleNamespace(key="mgmt"),
    ])
    boxes = {workspace: object() for workspace in ("tyit", "mgmt")}
    for workspace, box in boxes.items():
        factory(
            SimpleNamespace(key="hermes"), workspace=workspace,
            authorization_id=f"{workspace}:member", toolbox=box,
        )
    for key, kwargs in captured:
        assert key == "hermes"
        workspace = kwargs["workspace"]
        assert kwargs["authorization_id"] == f"{workspace}:member"
        assert kwargs["toolbox"] is boxes[workspace]
        assert kwargs["credential_id"] == "hermes:shared"
        assert kwargs["transport"]._path == "/run/tybot-subbots/hermes-shared/http.sock"
