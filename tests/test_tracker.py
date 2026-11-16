"""
offline unit tests: python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import proposal_tracker as pt  # noqa: E402

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)

# shaped like Neutron #9: the title and summary sound harmless, but the proposal runs MsgUpdateAdmin.
NEUTRON_LIKE = {
    "id": "9",
    "status": "PROPOSAL_STATUS_VOTING_PERIOD",
    "title": "Signaling proposal: community appreciation",
    "summary": "This is a text proposal, no on-chain changes.",
    "messages": [{"@type": "/cosmwasm.wasm.v1.MsgUpdateAdmin", "sender": "x", "new_admin": "attacker", "contract": "c"}],
    "submit_time": "2026-11-10T11:54:43.820711698Z",
    "deposit_end_time": "2026-11-24T11:54:43.820711698Z",
    "voting_start_time": "2026-11-10T11:57:22.597335274Z",
    "voting_end_time": "2026-11-17T11:57:22.597335274Z",
    "expedited": True,
}

TEXT = {"id": "1", "status": "PROPOSAL_STATUS_VOTING_PERIOD", "title": "Hello", "summary": "<b>x</b> & y",
        "messages": [], "voting_start_time": None, "voting_end_time": "0001-01-01T00:00:00Z"}

class TestTypes(unittest.TestCase):
    def test_text(self):
        self.assertEqual(pt.extract_types([]), ["Text"])
        self.assertEqual(pt.risk_flags(TEXT), [])

    def test_legacy_text_wrapped(self):
        msgs = [{"@type": "/cosmos.gov.v1.MsgExecLegacyContent", "content": {"@type": "/cosmos.gov.v1beta1.TextProposal"}}]
        self.assertTrue(pt.is_text_only(msgs))
        self.assertEqual(pt.extract_types(msgs), ["/cosmos.gov.v1.MsgExecLegacyContent → /cosmos.gov.v1beta1.TextProposal"])

    def test_authz_inner(self):
        msgs = [{"@type": "/cosmos.authz.v1beta1.MsgExec", "msgs": [{"@type": "/cosmos.bank.v1beta1.MsgSend"}]}]
        self.assertEqual(pt.extract_types(msgs), ["/cosmos.authz.v1beta1.MsgExec → /cosmos.bank.v1beta1.MsgSend"])
        flags = pt.risk_flags({"messages": msgs})
        self.assertTrue(any("MsgSend" in f for f in flags))
        self.assertTrue(any("authz" in f for f in flags))

    def test_any_update_params_is_high_risk(self):
        flags = pt.risk_flags({"messages": [{"@type": "/cosmos.staking.v1beta1.MsgUpdateParams"}]})
        self.assertTrue(flags[0].startswith("🚨"))

    def test_neutron_like_mismatch(self):
        flags = pt.risk_flags(NEUTRON_LIKE)
        self.assertTrue(flags[0].startswith("🚨"))
        self.assertTrue(any("signaling" in f for f in flags))

class TestMessage(unittest.TestCase):
    def test_header_is_type_and_escaped(self):
        msg = pt.build_message(NEUTRON_LIKE, NOW)
        first_lines = msg.splitlines()[:2]
        self.assertIn("proposal_type", first_lines[0])
        self.assertIn("/cosmwasm.wasm.v1.MsgUpdateAdmin", first_lines[1])
        self.assertIn("EXPEDITED", msg)
        self.assertIn("(in 1d 11h)", msg)
        self.assertLessEqual(len(msg), pt.TELEGRAM_MAX_CHARS)

    def test_html_escaping_and_null_times(self):
        msg = pt.build_message(TEXT, NOW)
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; y", msg)
        self.assertIn("voting_end_time:</b> -", msg)

    def test_nanosecond_time(self):
        self.assertEqual(pt.fmt_time("2026-11-17T11:57:22.597335274Z"), "2026-11-17 11:57:22 UTC")

class TestFailover(unittest.TestCase):
    def test_second_endpoint_used(self):
        def fake_get(url, timeout):
            if url.startswith("https://bad"):
                raise OSError("connection refused")
            if "node_info" in url:
                return {"default_node_info": {"network": "cosmoshub-4"}}
            return {"proposals": [NEUTRON_LIKE]}
        with mock.patch.object(pt, "http_get_json", side_effect=fake_get):
            res = pt.fetch_proposals(["https://bad", "https://good"], "cosmoshub-4", 1)
        self.assertEqual(res.endpoint, "https://good")
        self.assertEqual(len(res.errors), 1)

    def test_wrong_chain_rejected(self):
        with mock.patch.object(pt, "http_get_json", return_value={"default_node_info": {"network": "theta-testnet-001"}}):
            res = pt.fetch_proposals(["https://x"], "cosmoshub-4", 1)
        self.assertIsNone(res.proposals)

    def test_all_fail_sends_error_notice(self):
        sent = []
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "COSMOS_REST_ENDPOINTS": "https://a,https://b", "DRY_RUN": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(pt, "http_get_json", side_effect=OSError("down")), \
             mock.patch.object(pt.Notifier, "send", lambda self, text, html_mode=True, retries=3: (sent.append(text), (True, ""))[1]):
            rc = pt.main()
        self.assertEqual(rc, 1)
        self.assertEqual(len(sent), 1)
        self.assertIn("ALL endpoints failed", sent[0])

    def test_no_active_posts_nothing(self):
        sent = []
        def fake_get(url, timeout):
            if "node_info" in url:
                return {"default_node_info": {"network": "cosmoshub-4"}}
            return {"proposals": [dict(NEUTRON_LIKE, status="PROPOSAL_STATUS_PASSED")]}
        env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "c", "COSMOS_REST_ENDPOINTS": "https://a"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(pt, "http_get_json", side_effect=fake_get), \
             mock.patch.object(pt.Notifier, "send", lambda self, text, html_mode=True, retries=3: (sent.append(text), (True, ""))[1]):
            self.assertEqual(pt.main(), 0)
        self.assertEqual(sent, [])

if __name__ == "__main__":
    unittest.main()
