"""Was this message really sent by the domain in its From line?

Answered locally from the Authentication-Results header the receiving mail
server already wrote (Gmail, Outlook and Proton all do). No DNS lookups and no
network: MailSweep's only outbound calls stay localhost and approved unsubscribes.

The header is the receiver's verdict. We use the top-most one, which is the one
added last, by your own provider; a sender can't put a header above it.

    verified    DMARC passed, or DKIM/SPF passed for the From domain's own organisation
    failed      DMARC explicitly failed: the From line is very likely forged
    unverified  a verdict exists but nothing authenticated the From domain
                (common for small senders with no DMARC policy)
    unknown     no Authentication-Results header at all
"""
from __future__ import annotations

import re
from email.utils import parseaddr

_CC_SLD = {"co", "com", "org", "net", "gov", "edu", "ac"}
_COMMENT = re.compile(r"\([^)]*\)")
_METHOD = re.compile(r"^\s*(dkim|spf|dmarc)\s*=\s*([a-z]+)\b(.*)$", re.I | re.S)
_PROP = re.compile(r'([A-Za-z0-9_.\-]+)\s*=\s*("[^"]*"|[^\s;]+)')


def site(host_or_domain: str) -> str:
    """Rough registrable domain (last two labels, three for co.uk-style). Enough to
    tell 'the same organisation's own mail systems' from 'somebody else'."""
    p = (host_or_domain or "").lower().strip(".@ ").split(".")
    if len(p) >= 3 and p[-2] in _CC_SLD and len(p[-1]) == 2:
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _domain_of(value: str) -> str:
    v = value.strip('"').lower()
    return v.rpartition("@")[2] if "@" in v else v


def parse_authentication_results(value: str) -> dict:
    """{'dmarc': result|None, 'dkim': [(result, domain)], 'spf': [(result, domain)]}.
    Tolerates Outlook's form, which omits the leading authserv-id."""
    out: dict = {"dmarc": None, "dkim": [], "spf": []}
    for seg in _COMMENT.sub(" ", value or "").split(";"):
        m = _METHOD.match(seg)
        if not m:
            continue
        method, result = m.group(1).lower(), m.group(2).lower()
        props = {k.lower(): v for k, v in _PROP.findall(m.group(3))}
        if method == "dmarc":
            out["dmarc"] = result
        elif method == "dkim":
            d = props.get("header.d") or props.get("header.i") or ""
            out["dkim"].append((result, _domain_of(d)))
        else:
            d = props.get("smtp.mailfrom") or props.get("smtp.helo") or ""
            out["spf"].append((result, _domain_of(d)))
    return out


def assess(headers: dict[str, str]) -> tuple[str, str]:
    """(state, detail) for one message. `headers` is Message.headers (lower-case keys)."""
    ar = headers.get("authentication-results")
    if not ar:
        return "unknown", "no Authentication-Results header"
    res = parse_authentication_results(ar)
    dmarc = res["dmarc"]
    if dmarc in ("pass", "bestguesspass"):
        return "verified", f"dmarc={dmarc}"
    if dmarc == "fail":
        return "failed", "dmarc=fail: the From address is very likely forged"
    frm = site(_domain_of(parseaddr(headers.get("from", ""))[1]))
    for kind in ("dkim", "spf"):
        for result, dom in res[kind]:
            if result == "pass" and dom and frm and site(dom) == frm:
                return "verified", f"{kind} passed for {frm}"
    seen = sorted({site(d) for r, d in res["dkim"] + res["spf"] if r == "pass" and d})
    if seen and frm:
        return "unverified", f"authenticated as {', '.join(seen)}, not {frm}"
    return "unverified", f"no authentication for {frm or 'the From domain'}" + (f" (dmarc={dmarc})" if dmarc else "")


# How much a message's word is worth when two disagree about a sender's unsubscribe link.
RANK = {"verified": 3, "unknown": 2, "unverified": 1, "failed": 0}
