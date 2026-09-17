from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from platform_core.fetching import (
    FetchError,
    normalise,
    resolve,
    suggested_name,
)


def addrinfo(*literals):
    """socket.getaddrinfo-shaped records for the given address literals."""
    import socket

    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (literal, 443))
        for literal in literals
    ]


class FailureStatusTests(SimpleTestCase):
    """A caller that has to tell "not there" from "could not be read" needs the code.

    github_write.absent_path is the one that does: it may only report a path as
    absent on a real 404, because creating over a file that merely could not be
    read would be an overwrite of something nobody saw.
    """

    def test_an_http_failure_carries_its_status(self):
        self.assertEqual(FetchError("nope", status=404).status, 404)

    def test_a_failure_that_never_reached_a_response_carries_none(self):
        """Resolution, a private address, a redirect and the size caps all land here."""
        self.assertIsNone(FetchError("that host could not be resolved.").status)


class UrlValidationTests(SimpleTestCase):
    def test_a_bare_host_is_assumed_https(self):
        self.assertEqual(normalise("example.com/docs").scheme, "https")

    def test_only_http_and_https_are_accepted(self):
        for value in [
            "file:///etc/passwd",
            "ftp://example.com/x",
            "gopher://example.com",
            "data:text/html,<b>x</b>",
            "javascript:alert(1)",
        ]:
            with self.subTest(value=value), self.assertRaises(FetchError):
                normalise(value)

    def test_embedded_credentials_are_refused(self):
        """A URL carrying a password is usually an attempt to reach something private."""
        with self.assertRaises(FetchError):
            normalise("https://user:secret@example.com/page")

    def test_empty_and_oversized_links_are_refused(self):
        for value in ["", "   ", None, "https://example.com/" + "a" * 2100]:
            with self.subTest(value=value), self.assertRaises(FetchError):
                normalise(value)


@override_settings(FETCH_ALLOW_HOSTS=[])
class AddressPolicyTests(SimpleTestCase):
    """The SSRF boundary: what the server may be pointed at."""

    def test_public_addresses_resolve(self):
        with patch("socket.getaddrinfo", return_value=addrinfo("93.184.216.34")):
            self.assertEqual(resolve("example.com", 443)[0][1], "93.184.216.34")

    def test_private_and_reserved_ranges_are_refused(self):
        blocked = [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC1918
            "172.16.0.1",  # RFC1918
            "192.168.1.1",  # RFC1918
            "169.254.169.254",  # cloud metadata
            "0.0.0.0",  # unspecified
            "224.0.0.1",  # multicast
            "::1",  # IPv6 loopback
            "fd00::1",  # IPv6 unique local
        ]
        for literal in blocked:
            with self.subTest(address=literal):
                with patch("socket.getaddrinfo", return_value=addrinfo(literal)):
                    with self.assertRaises(FetchError) as raised:
                        resolve("internal.example", 443)
                    self.assertIn("private or reserved", str(raised.exception))

    def test_a_name_that_resolves_to_both_public_and_private_is_refused(self):
        """Any blocked answer poisons the whole result; one good record is not enough."""
        with patch("socket.getaddrinfo", return_value=addrinfo("93.184.216.34", "169.254.169.254")):
            with self.assertRaises(FetchError):
                resolve("split.example", 443)

    def test_an_unresolvable_host_is_reported_not_dialled(self):
        import socket as socket_module

        with patch("socket.getaddrinfo", side_effect=socket_module.gaierror):
            with self.assertRaises(FetchError):
                resolve("nx.example", 443)

    @override_settings(FETCH_ALLOW_HOSTS=["wiki.internal"])
    def test_an_operator_may_allow_one_named_internal_host(self):
        with patch("socket.getaddrinfo", return_value=addrinfo("10.0.0.5")):
            self.assertEqual(resolve("wiki.internal", 443)[0][1], "10.0.0.5")

    @override_settings(FETCH_ALLOW_HOSTS=["wiki.internal"])
    def test_allowing_one_host_does_not_allow_its_neighbours(self):
        with patch("socket.getaddrinfo", return_value=addrinfo("10.0.0.6")):
            with self.assertRaises(FetchError):
                resolve("other.internal", 443)

    @override_settings(FETCH_ALLOW_HOSTS=["WIKI.INTERNAL"])
    def test_the_allow_list_is_case_insensitive(self):
        with patch("socket.getaddrinfo", return_value=addrinfo("10.0.0.5")):
            self.assertEqual(resolve("wiki.internal", 443)[0][1], "10.0.0.5")


class FilenameTests(SimpleTestCase):
    def test_a_path_filename_is_kept(self):
        self.assertEqual(suggested_name(normalise("https://x.com/a/report.pdf"), ""), "report.pdf")

    def test_a_bare_page_takes_a_suffix_from_its_content_type(self):
        self.assertEqual(
            suggested_name(normalise("https://example.com/guide"), "text/html; charset=utf-8"),
            "guide.html",
        )
        self.assertEqual(
            suggested_name(normalise("https://example.com/data"), "text/csv"), "data.csv"
        )

    def test_a_root_url_falls_back_to_the_hostname(self):
        self.assertEqual(suggested_name(normalise("https://example.com/"), "text/html"),
                         "example.com")

    def test_separators_and_traversal_cannot_survive_into_a_filename(self):
        name = suggested_name(normalise("https://x.com/a/%2e%2e%2f%2e%2e%2fetc%2fpasswd"), "")
        self.assertNotIn("/", name)
        self.assertNotIn("..", name)

    def test_a_scheme_cannot_be_smuggled_past_the_https_default(self):
        """Regression: "data:..." has no "://" and once became "https://data:...".""" 
        for value in ["data:text/html,x", "javascript:alert(1)", "vbscript:x", "jar:file:///x"]:
            with self.subTest(value=value), self.assertRaises(FetchError):
                normalise(value)

    def test_a_host_with_a_port_and_no_scheme_still_works(self):
        parsed = normalise("example.com:8080/docs")
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "example.com")
        self.assertEqual(parsed.port, 8080)
