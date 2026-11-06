"""Authentication: protected secrets at rest, the two credential kinds, probe()."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from sipphone import config_store  # noqa: E402
from sipphone.authentication import protect, reveal, is_protected, UserCredentials, AdminCredentials, probe  # noqa: E402
from sipphone.cli import load_config  # noqa: E402
from tests.mock_xsi_events import MockXsi, USER, PW  # noqa: E402
from tests.test_presence_engine import TEAM  # noqa: E402


class CredentialStoreTests(unittest.TestCase):
    def test_round_trip_and_idempotence(self):
        for pw in ("s3cret", "p%ss w0rd", "ünïcødé", ""):
            stored = protect(pw)
            self.assertEqual(reveal(stored), pw)
            self.assertEqual(protect(stored), stored)                  # protecting twice changes nothing
            if pw:
                self.assertTrue(is_protected(stored)); self.assertNotIn(pw, stored)
        self.assertEqual(reveal("plain-old-value"), "plain-old-value")    # pre-2.4 configs still load
        self.assertEqual(reveal("b64:not base64!!"), "")

    def test_config_secrets_protected_on_save_plain_on_read(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "config.ini")
        shutil.copy(os.path.join(ROOT, "config.example.ini"), p)
        config_store.update(p, {"account": {"username": "u", "password": "sip-pw", "domain": "ex.com"},
                                "presence": {"xsi_password": "x-pw"}, "admin": {"password": "a-pw"}})
        raw = open(p).read()
        for pw in ("sip-pw", "x-pw", "a-pw"):
            self.assertNotIn(pw, raw)                                   # nothing plain on disk
        cp = config_store.read(p)
        self.assertEqual((cp["account"]["password"], cp["presence"]["xsi_password"], cp["admin"]["password"]),
                         ("sip-pw", "x-pw", "a-pw"))
        loaded = load_config(p)                                         # the app sees plain passwords
        self.assertEqual(loaded[0].password, "sip-pw")
        self.assertEqual(loaded[2]["xsi_password"], "x-pw")
        if sys.platform != "win32":
            self.assertEqual(oct(os.stat(p).st_mode)[-3:], "600")
        # a hand-edited plain password keeps working and is protected on the next save
        open(p, "a").write("\n")
        config_store.update(p, {"account": {"password": "plain-edit"}})
        self.assertEqual(config_store.read(p)["account"]["password"], "plain-edit")
        shutil.rmtree(d, ignore_errors=True)


class CredentialKindsTests(unittest.TestCase):
    def test_logins_and_seeds(self):
        u = UserCredentials("jdoe1234567", "pw", user_domain="ex.com")
        self.assertEqual((u.login, u.directory_seed, u.kind, u.complete), ("jdoe1234567@ex.com", "jdoe1234567@ex.com", "user", True))
        self.assertFalse(UserCredentials("jdoe", "").complete)
        a = AdminCredentials("xsi.1.admin", "pw", user_domain="ex.com", directory_user="alice")
        self.assertEqual((a.login, a.directory_seed, a.kind), ("xsi.1.admin", "alice@ex.com", "admin"))
        self.assertEqual(AdminCredentials("adm", "pw").directory_seed, "adm")

    def test_probe_reports_plainly(self):
        mock = MockXsi(TEAM, directory=TEAM + [("hg@nextiva.com", "Sales", "Hunt Group", "1100")])
        try:
            r = probe(UserCredentials(USER, PW, host=mock.base, verify_tls=False))
            self.assertTrue(r.ok, r.message); self.assertEqual((r.people, r.services), (3, 1))
            self.assertIn("decides who this login may watch", r.message)
            r = probe(UserCredentials(USER, "wrong", host=mock.base, verify_tls=False))
            self.assertFalse(r.ok); self.assertIn("not accepted", r.message)
            r = probe(UserCredentials(USER, "", host=mock.base, verify_tls=False))
            self.assertFalse(r.ok); self.assertIn("Enter the user login", r.message)
            r = probe(AdminCredentials(USER, PW, host=mock.base, verify_tls=False, directory_user="alice1001"))
            self.assertTrue(r.ok, r.message)
        finally:
            mock.stop()


if __name__ == "__main__":
    unittest.main()
