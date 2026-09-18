"""Is this container serving? Exit 0 for yes.

Asking over the loopback address is the obvious thing and the wrong one: the
Host header would then be 127.0.0.1, which is not in ALLOWED_HOSTS, so Django
answers 400 DisallowedHost. urlopen raises on 4xx, the check exits non-zero,
the container never reports healthy, and the proxy never routes to an
application that is working perfectly. It also writes a security ERROR to the
log every interval, which teaches whoever reads it to ignore that line.

So the request carries the first configured hostname, and a healthy answer is
the sign-in page rather than merely "something replied".
"""

import os
import sys
import urllib.error
import urllib.request


class KeepRedirect(urllib.request.HTTPRedirectHandler):
    """Treat a redirect as the answer, not as something to chase.

    In production SECURE_SSL_REDIRECT sends plain HTTP to https, so following
    the redirect takes this check out of the container, across the internet, to
    the public hostname - where it met the proxy in front of the very container
    it was trying to test and got that proxy's 503. A 301 from the application
    is proof the application answered, which is the whole question.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

PORT = os.environ.get("DIGITAL_BRAIN_PORT", "8000")
HOST = (os.environ.get("DIGITAL_BRAIN_HOSTS", "localhost").split(",")[0] or "localhost").strip()


def main():
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/accounts/login/",
        headers={"Host": HOST},
    )
    opener = urllib.request.build_opener(KeepRedirect)
    try:
        with opener.open(request, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as failure:
        # urllib raises for a redirect it was told not to follow, so a 3xx
        # arrives here rather than as a response. It is the healthy answer in
        # production: SECURE_SSL_REDIRECT is on, and only a working application
        # issues it.
        if 300 <= failure.code < 400:
            return 0
        # Anything else proves the stack answered but is not healthy. 400 here
        # almost always means this hostname is missing from DIGITAL_BRAIN_HOSTS.
        print(f"unhealthy: HTTP {failure.code} for Host: {HOST}", file=sys.stderr)
        return 1
    except Exception as failure:
        print(f"unhealthy: {type(failure).__name__}", file=sys.stderr)
        return 1
    # 2xx is the sign-in page; 3xx is the HTTPS redirect, which only a working
    # application produces.
    if status >= 400:
        print(f"unhealthy: HTTP {status}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
