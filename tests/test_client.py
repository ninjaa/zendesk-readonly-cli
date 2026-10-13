import base64
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request

SPEC = importlib.util.spec_from_file_location("zendesk_client", Path(__file__).resolve().parents[1] / "zendesk.py")
zd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(zd)
ORIGIN = "https://exampletenant.zendesk.com"


class ClientTests(unittest.TestCase):
    def test_subdomain_injection_rejected_before_network(self):
        for label in (None, "", "-tenant", "tenant-", "tenant.example", "tenant.zendesk.com", "evil/", "evil?x=y", "evil#fragment", "user@tenant", "tenant:443", "//evil", "https://evil", "tenant\n", "TENANT", "a" * 64, "ténant"):
            with self.subTest(label=label), patch.object(zd, "build_opener") as opener, self.assertRaises(zd.ClientError):
                zd.Zendesk(label, "Bearer synthetic")
            opener.assert_not_called()

    def test_valid_subdomains_fix_the_origin(self):
        for label in ("a", "sample-tenant", "1", "a" * 63):
            with self.subTest(label=label):
                client = zd.Zendesk(label, "Bearer synthetic")
                self.assertEqual(client.origin, "https://" + label + ".zendesk.com")

    def test_other_tenant_pagination_is_rejected(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.opener = Mock()
        client.opener.open.return_value = io.StringIO(json.dumps({"results": [], "meta": {"has_more": True}, "links": {"next": "https://othertenant.zendesk.com/api/v2/search/export.json"}}))
        with self.assertRaises(zd.ClientError):
            client.tickets()
        self.assertEqual(client.opener.open.call_count, 1)

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_subdomain_fails_before_credentials_or_prompt(self):
        with patch("sys.argv", ["zendesk.py", "--prompt-token", "--email", "agent@example.com", "me"]), patch("sys.stderr", new_callable=io.StringIO) as stderr, patch.object(zd, "credentials") as auth:
            self.assertEqual(zd.main(), 1)
            self.assertIn("subdomain", stderr.getvalue())
            auth.assert_not_called()

    def test_token_prompt_uses_memory_and_never_changes_configuration(self):
        values = {"ZENDESK_EMAIL": "agent@example.com", "ZENDESK_API_TOKEN": "old-synthetic-token"}
        with patch.object(zd.getpass, "getpass", return_value="new-synthetic-token") as prompt:
            header = zd.credentials(values, prompt_token=True)
        prompt.assert_called_once()
        self.assertEqual(base64.b64decode(header.removeprefix("Basic ")).decode(), "agent@example.com/token:new-synthetic-token")
        self.assertEqual(values["ZENDESK_API_TOKEN"], "old-synthetic-token")

    def test_token_prompt_requires_email_and_rejects_active_oauth(self):
        for values in ({}, {"ZENDESK_OAUTH_TOKEN": "synthetic-oauth", "ZENDESK_EMAIL": "agent@example.com"}):
            with self.subTest(values=values), patch.object(zd.getpass, "getpass") as prompt, self.assertRaises(zd.ClientError) as caught:
                zd.credentials(values, prompt_token=True)
            prompt.assert_not_called()
            self.assertNotIn("synthetic-oauth", str(caught.exception))

    def test_token_prompt_cannot_fall_back_to_echoed_input(self):
        def warn_instead_of_prompt(*args):
            zd.warnings.warn("Synthetic warning", zd.getpass.GetPassWarning)
        with patch.object(zd.getpass, "getpass", side_effect=warn_instead_of_prompt), self.assertRaises(zd.ClientError):
            zd.credentials({"ZENDESK_EMAIL": "agent@example.com"}, prompt_token=True)

    @patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": "environmenttenant", "ZENDESK_OAUTH_TOKEN": "synthetic-oauth"}, clear=True)
    def test_cli_subdomain_overrides_environment_and_keeps_tokens_out_of_output(self):
        with patch("sys.argv", ["zendesk.py", "--subdomain", "explicittenant", "me"]), patch.object(zd.Zendesk, "get", autospec=True, return_value={"user": {"id": 1, "name": "Synthetic", "credentials": "synthetic-secret"}}) as get, patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(zd.main(), 0)
            self.assertNotIn("synthetic-oauth", stdout.getvalue())
            self.assertNotIn("synthetic-secret", stdout.getvalue())
            self.assertEqual(get.call_args.args[0].hostname, "explicittenant.zendesk.com")
            self.assertEqual(get.call_args.args[1], "/api/v2/users/me.json")

    @patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": "environmenttenant", "ZENDESK_OAUTH_TOKEN": "synthetic-oauth"}, clear=True)
    def test_cli_accepts_environment_subdomain(self):
        with patch("sys.argv", ["zendesk.py", "me"]), patch.object(zd, "Zendesk") as client_class, patch("sys.stdout", new_callable=io.StringIO):
            client_class.return_value.get.return_value = {"user": {"id": 1}}
            self.assertEqual(zd.main(), 0)
            client_class.assert_called_once_with("environmenttenant", "Bearer synthetic-oauth")

    @patch.dict(os.environ, {}, clear=True)
    def test_credentials_read_only_selected_keys_without_executing_env(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('OTHER_SECRET=ignore\nZENDESK_API_TOKEN="synthetic-token"\nZENDESK_EMAIL=agent@example.com\n')
            header = zd.credentials(zd.configuration(path))
            self.assertEqual(base64.b64decode(header.removeprefix("Basic ")).decode(), "agent@example.com/token:synthetic-token")
            self.assertEqual(path.read_text(), 'OTHER_SECRET=ignore\nZENDESK_API_TOKEN="synthetic-token"\nZENDESK_EMAIL=agent@example.com\n')

    @patch.dict(os.environ, {"ZENDESK_OAUTH_TOKEN": "synthetic-oauth", "ZENDESK_API_TOKEN": "other"}, clear=True)
    def test_oauth_takes_precedence(self):
        self.assertEqual(zd.credentials(zd.configuration()), "Bearer synthetic-oauth")

    @patch.dict(os.environ, {"ZENDESK_OAUTH_TOKEN": "synthetic\nsecret"}, clear=True)
    def test_malformed_secret_is_not_printed(self):
        with self.assertRaises(zd.ClientError) as caught:
            zd.credentials(zd.configuration())
        self.assertNotIn("synthetic", str(caught.exception))

    def test_requests_are_get_only_with_timeout(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        response = io.StringIO(json.dumps({"user": {"id": 1}}))
        client.opener = Mock()
        client.opener.open.return_value = response
        self.assertEqual(client.get("/api/v2/users/me.json"), {"user": {"id": 1}})
        req = client.opener.open.call_args.args[0]
        self.assertEqual(req.get_method(), "GET")
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic")
        self.assertEqual(client.opener.open.call_args.kwargs, {"timeout": 30})

    def test_foreign_and_non_api_urls_rejected_before_network(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.opener = Mock()
        for path in ["https://evil.example/api/v2/users/me.json", "//evil.example/api/v2/users/me.json",
                     "http://exampletenant.zendesk.com/api/v2/users/me.json", "/admin", "/api/v2/oauth/clients.json",
                     "https://agent@exampletenant.zendesk.com/api/v2/users/me.json", "/api/v2/users/me.json#fragment"]:
            with self.subTest(path=path), self.assertRaises(zd.ClientError):
                client.get(path)
        client.opener.open.assert_not_called()

    def test_redirect_cannot_forward_credentials(self):
        handler = zd.NoRedirects()
        for status in (301, 302, 303, 307, 308):
            request = Request(ORIGIN + "/api/v2/users/me.json", headers={"Authorization": "Bearer synthetic"})
            with self.subTest(status=status), self.assertRaises(zd.ClientError):
                getattr(handler, f"http_error_{status}")(request, io.BytesIO(), status, "Redirect", {"location": "https://evil.example"})

    def test_http_errors_never_print_response_or_secret(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.opener = Mock()
        for status in (401, 429, 500):
            body = io.BytesIO(b"synthetic-secret")
            client.opener.open.side_effect = HTTPError(ORIGIN, status, "synthetic-secret", {}, body)
            with self.subTest(status=status), self.assertRaises(zd.ClientError) as caught:
                client.get("/api/v2/users/me.json")
            self.assertNotIn("synthetic-secret", str(caught.exception))
            self.assertTrue(body.closed)

    def test_cursor_pagination_and_latest_first(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(side_effect=[
            {"results": [{"id": 1, "updated_at": "2026-01-01"}], "meta": {"has_more": True}, "links": {"next": ORIGIN + "/api/v2/search/export.json?page=2"}},
            {"results": [{"id": 2, "updated_at": "2026-02-01"}], "meta": {"has_more": False}, "links": {"next": "unused"}},
        ])
        self.assertEqual([row["id"] for row in client.tickets()], [2, 1])
        self.assertIn("status%3C%3Dopen", client.get.call_args_list[0].args[0])
        self.assertEqual(client.get.call_count, 2)

    def test_cursor_missing_next_page_fails_instead_of_partial_result(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(return_value={"results": [], "meta": {"has_more": True}, "links": {}})
        with self.assertRaises(zd.ClientError):
            client.tickets()

    def test_missing_or_malformed_pagination_markers_fail(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        for markers in ({}, {"meta": {}}, {"meta": None}, {"meta": {"has_more": None}},
                        {"meta": {"has_more": "false"}}, {"next_page": ""}, {"next_page": 2}):
            client.get = Mock(return_value={"results": [], **markers})
            with self.subTest(markers=markers), self.assertRaises(zd.ClientError):
                client.pages("/api/v2/search/export.json", "results")

    def test_page_limit_fails_without_claiming_complete_results(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(side_effect=lambda path: {"comments": [], "next_page": f"/api/v2/tickets/1/comments.json?page={client.get.call_count + 1}"})
        with self.assertRaises(zd.ClientError):
            client.pages("/api/v2/tickets/1/comments.json", "comments")
        self.assertEqual(client.get.call_count, 200)

    def test_pagination_loop_is_detected(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(return_value={"comments": [], "next_page": ORIGIN + "/api/v2/tickets/1/comments.json"})
        with self.assertRaises(zd.ClientError):
            client.pages("/api/v2/tickets/1/comments.json", "comments")
        self.assertEqual(client.get.call_count, 1)

    def test_foreign_pagination_is_rejected_before_second_request(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.opener = Mock()
        client.opener.open.return_value = io.StringIO(json.dumps({"results": [], "meta": {"has_more": True}, "links": {"next": "https://evil.example/api/v2/search/export"}}))
        with self.assertRaises(zd.ClientError):
            client.tickets()
        self.assertEqual(client.opener.open.call_count, 1)

    def test_thread_fetches_all_comments_and_author_names(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(side_effect=[
            {"ticket": {"id": 1, "subject": "Synthetic"}},
            {"comments": [{"id": 1, "author_id": 10, "body": "Question", "public": True}], "next_page": "/api/v2/tickets/1/comments.json?page=2"},
            {"comments": [{"id": 2, "author_id": 20, "body": "Reply", "public": True}], "next_page": None},
            {"users": [{"id": 10, "name": "Customer", "role": "end-user"}, {"id": 20, "name": "Agent", "role": "agent"}]},
        ])
        thread = client.thread(1)
        self.assertEqual([row["body"] for row in thread["comments"]], ["Question", "Reply"])
        self.assertEqual([row["name"] for row in thread["authors"]], ["Customer", "Agent"])

    def test_author_batches_and_missing_authors_are_explicit(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(side_effect=[
            {"ticket": {"id": 1}},
            {"comments": [{"author_id": i} for i in range(1, 102)], "next_page": None},
            {"users": [{"id": i, "name": "Synthetic"} for i in range(1, 101)]},
            {"users": []},
        ])
        thread = client.thread(1)
        self.assertEqual(len(thread["authors"]), 100)
        self.assertEqual(thread["unresolved_author_ids"], [101])
        self.assertEqual(client.get.call_count, 4)

    def test_malformed_author_response_is_rejected(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.get = Mock(side_effect=[{"ticket": {"id": 1}}, {"comments": [{"author_id": 1}], "next_page": None}, {"users": "invalid"}])
        with self.assertRaises(zd.ClientError):
            client.thread(1)

    def test_invalid_json_error_is_sanitized(self):
        client = zd.Zendesk("exampletenant", "Bearer synthetic")
        client.opener = Mock()
        client.opener.open.return_value = io.StringIO("synthetic-secret is not JSON")
        with self.assertRaises(zd.ClientError) as caught:
            client.get("/api/v2/users/me.json")
        self.assertNotIn("synthetic-secret", str(caught.exception))

    @patch.dict(os.environ, {}, clear=True)
    def test_cli_missing_credentials_and_file_errors_are_sanitized(self):
        for argv in (["zendesk.py", "--subdomain", "exampletenant", "me"], ["zendesk.py", "--subdomain", "exampletenant", "--env-file", "/nonexistent/synthetic-secret", "me"]):
            with self.subTest(argv=argv), patch("sys.argv", argv), patch("sys.stderr", new_callable=io.StringIO) as stderr:
                self.assertEqual(zd.main(), 1)
                self.assertNotIn("synthetic-secret", stderr.getvalue())

    def test_cli_invalid_id_fails_before_authentication(self):
        with patch("sys.argv", ["zendesk.py", "thread", "../2"]), patch("sys.stderr", new_callable=io.StringIO), patch.object(zd, "credentials") as auth:
            with self.assertRaises(SystemExit) as caught:
                zd.main()
            self.assertEqual(caught.exception.code, 2)
            auth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
