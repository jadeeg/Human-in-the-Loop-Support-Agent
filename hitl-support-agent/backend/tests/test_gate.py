"""Safety-critical tests: the approval gate must hold no matter what the LLM asks for.
Run from the repo root:  python -m pytest backend/tests   (or: python -m unittest discover -s backend/tests -t .)
"""
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from backend import agent, config, db, rag, risk, tools
from backend.mock_llm import mock_llm


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = str(Path(self.tmp.name) / "test.db")
        db.reset()
        self.sid = "s1"
        db.ensure_session(self.sid)

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, order_id="12345", email="maria@example.com", sid=None):
        return risk.request_tool("check_order", {"order_id": order_id, "email": email}, sid or self.sid)

    def events(self, sid=None):
        return [e["event"] for e in db.get_audit(sid or self.sid)]


class TestLowRisk(Base):
    def test_check_order_ok(self):
        r = self.verify()
        self.assertEqual(r["order"]["order_id"], "12345")
        self.assertTrue(r["cancellation"]["eligible"])

    def test_wrong_email_same_error_as_missing_order(self):
        a = self.verify("12345", "ana@example.com")
        b = self.verify("99999", "maria@example.com")
        self.assertEqual(a, b)
        self.assertEqual(a["error"], "verification_failed")

    def test_unknown_tool_rejected(self):
        self.assertEqual(risk.request_tool("drop_database", {}, self.sid)["error"], "unknown_tool")
        self.assertEqual(risk.request_tool("delete_account", {}, self.sid)["error"], "unknown_tool")


class TestGate(Base):
    def snapshot(self):
        return [(o["order_id"], o["status"], o["refunded_amount"]) for o in db.all_orders()]

    def test_high_risk_requires_verification(self):
        r = risk.request_tool("cancel_order", {"order_id": "12345"}, self.sid)
        self.assertEqual(r["error"], "not_verified")
        self.assertEqual(db.list_approvals("all"), [])

    def test_cannot_act_on_someone_elses_order(self):
        self.verify("12345", "maria@example.com")
        r = risk.request_tool("cancel_order", {"order_id": "12351"}, self.sid)
        self.assertEqual(r["error"], "not_verified")

    def test_high_risk_never_executes_directly(self):
        before = self.snapshot()
        self.verify()
        r = risk.request_tool("cancel_order", {"order_id": "12345", "reason": "changed my mind"}, self.sid)
        self.assertEqual(r["status"], "pending_approval")
        self.assertEqual(self.snapshot(), before)

    def test_no_llm_call_can_mutate_data(self):
        """Property: whatever high-risk tool/args the model emits, orders stay untouched until approve()."""
        self.verify("12348", "joao@example.com", sid="a")
        self.verify("12345", "maria@example.com", sid="b")
        before = self.snapshot()
        for sid, oid in (("a", "12348"), ("b", "12345")):
            for tool in ("cancel_order", "issue_refund"):
                for args in ({"order_id": oid}, {"order_id": oid, "amount": 9999}, {"order_id": oid, "amount": -5}, {"order_id": oid, "amount": "abc"}):
                    risk.request_tool(tool, args, sid)
        self.assertEqual(self.snapshot(), before)

    def test_ineligible_is_denied_without_request(self):
        self.verify("12346", "maria@example.com")
        r = risk.request_tool("cancel_order", {"order_id": "12346"}, self.sid)
        self.assertEqual(r["status"], "denied")
        self.assertEqual(db.list_approvals("all"), [])

    def test_refund_amount_validated(self):
        self.verify("12348", "joao@example.com")
        for bad in (9999, -1, 0, "abc"):
            r = risk.request_tool("issue_refund", {"order_id": "12348", "amount": bad}, self.sid)
            self.assertEqual(r["status"], "denied", bad)
        self.assertEqual(db.list_approvals("all"), [])

    def test_duplicate_pending_request_reuses_same_id(self):
        self.verify()
        a = risk.request_tool("cancel_order", {"order_id": "12345"}, self.sid)
        b = risk.request_tool("cancel_order", {"order_id": "12345"}, self.sid)
        self.assertEqual(a["request_id"], b["request_id"])
        self.assertEqual(len(db.list_approvals("all")), 1)

    def test_request_carries_policy_evidence_for_reviewer(self):
        self.verify()
        rid = risk.request_tool("cancel_order", {"order_id": "12345"}, self.sid)["request_id"]
        s = db.get_approval(rid)["summary"]
        self.assertEqual(s["amount"], 149.00)
        self.assertTrue(s["policy_decision"]["eligible"])
        self.assertIn("cancellation_policy.md", [p["source"] for p in s["policy_passages"]])


class TestDecisions(Base):
    def request(self, tool="cancel_order", oid="12345", email="maria@example.com"):
        self.verify(oid, email)
        return risk.request_tool(tool, {"order_id": oid}, self.sid)["request_id"]

    def test_approve_cancel_executes_once(self):
        rid = self.request()
        r = risk.approve(rid)
        self.assertEqual(r["status"], "approved")
        o = db.get_order("12345")
        self.assertEqual((o["status"], o["refunded_amount"]), ("cancelled", 149.0))
        self.assertIn("cancelled", db.visible_messages(self.sid)[-1]["content"])

    def test_double_approve_does_not_double_refund(self):
        rid = self.request("issue_refund", "12348", "joao@example.com")
        self.assertEqual(risk.approve(rid)["status"], "approved")
        self.assertEqual(risk.approve(rid)["error"], "not_pending")
        self.assertEqual(db.get_order("12348")["refunded_amount"], 120.0)

    def test_reject_leaves_order_untouched_and_notifies(self):
        rid = self.request()
        self.assertEqual(risk.reject(rid, "Order already packed")["status"], "rejected")
        self.assertEqual(db.get_order("12345")["status"], "processing")
        self.assertIn("Order already packed", db.visible_messages(self.sid)[-1]["content"])
        self.assertEqual(risk.approve(rid)["error"], "not_pending")

    def test_expired_request_cannot_be_approved(self):
        rid = self.request()
        later = db.now() + timedelta(minutes=config.APPROVAL_TTL_MINUTES + 1)
        with patch("backend.db.now", return_value=later):
            self.assertEqual(risk.approve(rid)["error"], "expired")
        self.assertEqual(db.get_order("12345")["status"], "processing")

    def test_state_change_between_request_and_approval(self):
        rid = self.request()
        with db.tx() as c:
            c.execute("UPDATE orders SET status='shipped' WHERE order_id='12345'")
        r = risk.approve(rid)
        self.assertEqual(r["error"], "recheck_failed")
        self.assertEqual(db.get_order("12345")["status"], "shipped")
        self.assertEqual(db.get_approval(rid)["status"], "failed")

    def test_audit_trail_tells_the_whole_story(self):
        rid = self.request()
        risk.approve(rid)
        ev = self.events()
        for expected in ("TOOL_REQUESTED", "CUSTOMER_VERIFIED", "ACTION_CLASSIFIED_HIGH_RISK", "POLICY_EVALUATED",
                         "APPROVAL_REQUESTED", "APPROVED", "ACTION_EXECUTED"):
            self.assertIn(expected, ev)
        self.assertLess(ev.index("APPROVAL_REQUESTED"), ev.index("APPROVED"))
        self.assertLess(ev.index("APPROVED"), ev.index("ACTION_EXECUTED"))


class TestAgentEndToEnd(Base):
    def test_cancel_flow_with_confirmation_and_approval(self):
        sid = "e2e"
        out = agent.run_turn(sid, "Cancel order 12345, my email is maria@example.com", llm=mock_llm)
        self.assertIn("submit the cancellation request", out["reply"])
        self.assertIsNone(out["pending_request_id"])

        out = agent.run_turn(sid, "Yes", llm=mock_llm)
        self.assertIsNotNone(out["pending_request_id"])
        self.assertIn("specialist needs to approve", out["reply"])
        self.assertEqual(db.get_order("12345")["status"], "processing")

        risk.approve(out["pending_request_id"])
        self.assertEqual(db.get_order("12345")["status"], "cancelled")
        self.assertIn("has been cancelled", db.visible_messages(sid)[-1]["content"])

    def test_multi_turn_slot_filling(self):
        sid = "slots"
        self.assertIn("order number", agent.run_turn(sid, "I want to cancel", llm=mock_llm)["reply"])
        self.assertIn("email", agent.run_turn(sid, "12345", llm=mock_llm)["reply"])
        self.assertIn("submit the cancellation request", agent.run_turn(sid, "maria@example.com", llm=mock_llm)["reply"])

    def test_model_cannot_fake_an_approval_message(self):
        """Even if the model claims success, the customer sees the system's wording."""
        sid = "liar"
        agent.run_turn(sid, "Cancel order 12345, my email is maria@example.com", llm=mock_llm)

        def lying_llm(messages, schemas):
            if messages[-1]["role"] == "tool":
                return {"role": "assistant", "content": "Done! Your order is cancelled and refunded."}
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "cancel_order", "arguments": {"order_id": "12345"}}}]}

        out = agent.run_turn(sid, "yes", llm=lying_llm)
        self.assertNotIn("is cancelled and refunded", out["reply"])
        self.assertIn("approve", out["reply"])
        self.assertEqual(db.get_order("12345")["status"], "processing")

    def test_llm_failure_escalates_gracefully(self):
        def broken(messages, schemas):
            raise ConnectionError("ollama down")

        out = agent.run_turn("err", "hi", llm=broken)
        self.assertIn("specialist will follow up", out["reply"])
        self.assertIn("LLM_ERROR", self.events("err"))

    def test_runaway_tool_loop_is_capped(self):
        def looping(messages, schemas):
            return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "check_policy", "arguments": {"topic": "refund"}}}]}

        out = agent.run_turn("loop", "hi", llm=looping)
        self.assertIn("MAX_STEPS_REACHED", self.events("loop"))
        self.assertTrue(out["reply"])


class TestRag(unittest.TestCase):
    def test_retrieval_finds_the_right_policy(self):
        self.assertEqual(rag.retrieve("can I cancel after it shipped")[0]["source"], "cancellation_policy.md")
        self.assertEqual(rag.retrieve("how many days to get a refund after delivery")[0]["source"], "refund_policy.md")
        self.assertEqual(rag.retrieve("how long does delivery take tracking")[0]["source"], "shipping_policy.md")


if __name__ == "__main__":
    unittest.main()
