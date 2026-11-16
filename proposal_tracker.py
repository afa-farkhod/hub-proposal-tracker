#!/usr/bin/env python3

from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Public Cosmos Hub REST (LCD / gRPC-gateway) endpoints, tried in order
# Override with COSMOS_REST_ENDPOINTS env variable / GitHub variable
DEFAULT_ENDPOINTS = [
    "https://cosmos-rest.publicnode.com",
    "https://rest.cosmos.directory/cosmoshub",
    "https://cosmos-api.polkachu.com",
    "https://rest-cosmoshub.ecostake.com",
    "https://api.cosmos.nodestake.org",
]

DEFAULT_STATUSES = "PROPOSAL_STATUS_VOTING_PERIOD,PROPOSAL_STATUS_DEPOSIT_PERIOD"

# How many of the most recent proposals to inspect. Active proposals are always
# among the newest ones, so 100 is far more than enough
# NOTE: deliberately NOT used the "proposal_status=" server-side filter:
# on cosmoshub-4 it fails with "no concrete type registered for type URL
# "/cosmos.params.v1beta1.ParameterChangeProposal" because it walks old
# proposals that the current binary can no longer decode
# should be updated after v29.1.0 version lands the mainnet
RECENT_LIMIT = 100

PROPOSALS_PATH = (
    f"/cosmos/gov/v1/proposals?pagination.reverse=true&pagination.limit={RECENT_LIMIT}"
)
NODE_INFO_PATH = "/cosmos/base/tendermint/v1beta1/node_info"

MINTSCAN_URL = "https://www.mintscan.io/cosmos/proposals/{id}"
PINGPUB_URL = "https://ping.pub/cosmos/gov/{id}"

SUMMARY_MAX_CHARS = 700
TELEGRAM_MAX_CHARS = 4096
USER_AGENT = "cosmoshub-proposal-tracker/1.0"

# Message types that can move funds, change code/admin rights, change chain
# params or act on behalf of other accounts. Proposals containing them get a
# 🚨 marker. Anything that isn't a plain Text proposal is still worth reading.
HIGH_RISK_TYPES = {
    # CosmWasm admin / code changes (the Neutron #9 vector)
    "/cosmwasm.wasm.v1.MsgUpdateAdmin",
    "/cosmwasm.wasm.v1.MsgClearAdmin",
    "/cosmwasm.wasm.v1.MsgMigrateContract",
    "/cosmwasm.wasm.v1.MsgExecuteContract",
    "/cosmwasm.wasm.v1.MsgStoreCode",
    "/cosmwasm.wasm.v1.MsgInstantiateContract",
    "/cosmwasm.wasm.v1.MsgInstantiateContract2",
    "/cosmwasm.wasm.v1.MsgSudoContract",
    "/cosmwasm.wasm.v1.MsgPinCodes",
    "/cosmwasm.wasm.v1.MsgStoreAndInstantiateContract",
    "/cosmwasm.wasm.v1.MsgUpdateInstantiateConfig",
    "/cosmwasm.wasm.v1.MsgAddCodeUploadParamsAddresses",
    # Acting on behalf of others
    "/cosmos.authz.v1beta1.MsgExec",
    "/cosmos.authz.v1beta1.MsgGrant",
    "/cosmos.feegrant.v1beta1.MsgGrantAllowance",
    # Moving funds
    "/cosmos.bank.v1beta1.MsgSend",
    "/cosmos.bank.v1beta1.MsgMultiSend",
    "/cosmos.distribution.v1beta1.MsgCommunityPoolSpend",
    "/cosmos.distribution.v1beta1.MsgSetWithdrawAddress",
    "/cosmos.protocolpool.v1.MsgCommunityPoolSpend",
    "/cosmos.protocolpool.v1.MsgCreateContinuousFund",
    "/ibc.applications.transfer.v1.MsgTransfer",
    "/cosmos.gov.v1beta1.CommunityPoolSpendProposal",
    "/cosmos.distribution.v1beta1.CommunityPoolSpendProposal",
    # Chain upgrades / IBC client surgery
    "/cosmos.upgrade.v1beta1.MsgSoftwareUpgrade",
    "/cosmos.upgrade.v1beta1.MsgCancelUpgrade",
    "/ibc.core.client.v1.MsgIBCSoftwareUpgrade",
    "/ibc.core.client.v1.MsgRecoverClient",
    "/ibc.core.client.v1.MsgUpdateClient",
    "/ibc.core.client.v1.MsgUpgradeClient",
    "/cosmos.upgrade.v1beta1.SoftwareUpgradeProposal",
    "/ibc.core.client.v1.ClientUpdateProposal",
    "/ibc.core.client.v1.UpgradeProposal",
    # Consensus / staking / slashing / gov parameter changes
    "/cosmos.consensus.v1.MsgUpdateParams",
    "/cosmos.params.v1beta1.ParameterChangeProposal",
}

# Any MsgUpdateParams of any module is treated as high-risk too
HIGH_RISK_SUFFIXES = ("MsgUpdateParams",)

# Wrapper messages whose *inner* messages are what actually executes
WRAPPER_TYPES = {
    "/cosmos.gov.v1.MsgExecLegacyContent",  # field: content
    "/cosmos.authz.v1beta1.MsgExec",        # field: msgs
}

TEXT_TYPE = "Text"
LEGACY_TEXT_TYPES = {"/cosmos.gov.v1beta1.TextProposal"}

# Words in title/summary suggesting the author presents it as a text-only
# (signaling) proposal. If such a proposal carries executable messages,
# flag a possible description/type mismatch
TEXT_CLAIM_WORDS = ("signaling", "signalling", "text proposal", "non-binding", "no code", "no on-chain")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "y", "on")

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] {msg}", flush=True)

def http_get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)

def http_post_json(url: str, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Telegram returns JSON body describing the error; surface it
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error_code": e.code, "description": str(e)}

def parse_time(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    v = value.strip().replace("Z", "+00:00")
    # Trim nanoseconds to microseconds for "fromisoformat"
    if "." in v:
        head, tail = v.split(".", 1)
        frac, tz = "", ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                frac, tz = tail[:i], tail[i:]
                break
        else:
            frac = tail
        v = f"{head}.{frac[:6]}{tz}"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def fmt_time(value: str | None) -> str:
    dt = parse_time(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else "-"

def fmt_remaining(value: str | None, now: datetime) -> str:
    dt = parse_time(value)
    if not dt:
        return ""
    secs = int((dt - now).total_seconds())
    if secs <= 0:
        return " (ended)"
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    return f" (in {days}d {hours}h)" if days else f" (in {hours}h {(rem % 3600) // 60}m)"

def esc(s: str) -> str:
    return html.escape(s or "", quote=False)

def truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

# ---------------------------------------------------------------------------
# Proposal analysis
# ---------------------------------------------------------------------------

def extract_types(messages: list[dict]) -> list[str]:
    """
    Return human-readable proposal types. Wrapper messages are expanded so
    that the actually executed type is visible, e.g.
      "/cosmos.authz.v1beta1.MsgExec → /cosmos.bank.v1beta1.MsgSend"
    Proposal without messages is plain "Text" proposal
    """
    if not messages:
        return [TEXT_TYPE]
    out: list[str] = []
    for m in messages:
        t = m.get("@type", "<unknown>")
        if t == "/cosmos.gov.v1.MsgExecLegacyContent":
            inner = (m.get("content") or {}).get("@type", "<unknown>")
            out.append(f"{t} → {inner}")
        elif t == "/cosmos.authz.v1beta1.MsgExec":
            inner = [x.get("@type", "<unknown>") for x in (m.get("msgs") or [])] or ["<none>"]
            out.append(f"{t} → {', '.join(inner)}")
        else:
            out.append(t)
    return out

def all_type_urls(messages: list[dict]) -> set[str]:
    """
    Flat set of every type URL, including inner messages of wrappers
    """
    urls: set[str] = set()
    for m in messages or []:
        t = m.get("@type", "")
        urls.add(t)
        if t == "/cosmos.gov.v1.MsgExecLegacyContent":
            urls.add((m.get("content") or {}).get("@type", ""))
        elif t == "/cosmos.authz.v1beta1.MsgExec":
            for x in m.get("msgs") or []:
                urls.add(x.get("@type", ""))
    urls.discard("")
    return urls

def is_text_only(messages: list[dict]) -> bool:
    urls = all_type_urls(messages) - {"/cosmos.gov.v1.MsgExecLegacyContent"}
    return not urls or urls <= LEGACY_TEXT_TYPES

def risk_flags(p: dict) -> list[str]:
    messages = p.get("messages") or []
    flags: list[str] = []
    if is_text_only(messages):
        return flags
    urls = all_type_urls(messages)
    risky = sorted(u for u in urls if u in HIGH_RISK_TYPES or u.endswith(HIGH_RISK_SUFFIXES))
    if risky:
        flags.append("🚨 HIGH-RISK message type(s): " + ", ".join(risky))
    else:
        flags.append("⚠️ Executable proposal - verify the messages match the description")
    text = f"{p.get('title', '')}\n{p.get('summary', '')}".lower()
    if any(w in text for w in TEXT_CLAIM_WORDS):
        flags.append("🚩 Description reads like a text/signaling proposal, but it EXECUTES on-chain messages")
    if "/cosmos.authz.v1beta1.MsgExec" in urls:
        flags.append("🚩 Uses authz MsgExec - acts on behalf of another account")
    return flags

def build_message(p: dict, now: datetime) -> str:
    pid = p.get("id", "?")
    messages = p.get("messages") or []
    types = extract_types(messages)
    flags = risk_flags(p)
    status = p.get("status", "")
    short_status = status.replace("PROPOSAL_STATUS_", "")
    icon = "🚨" if any(f.startswith("🚨") for f in flags) else ("⚠️" if flags else "📝")

    lines: list[str] = []
    # Header: the proposal type(s) - the key detector
    lines.append(f"{icon} <b>proposal_type:</b>")
    for t in types:
        lines.append(f"<code>{esc(t)}</code>")
    lines.append("")
    lines.append(f"<b>Cosmos Hub #{esc(str(pid))}</b> - {esc(truncate(p.get('title', ''), 250))}")
    lines.append("")
    if flags:
        lines.extend(esc(f) for f in flags)
        lines.append("")
    lines.append(f"<b>proposal_status:</b> {esc(status)}"
                 + (" (⚡ EXPEDITED)" if p.get("expedited") else ""))
    lines.append(f"<b>submit_time:</b> {fmt_time(p.get('submit_time'))}")
    lines.append(f"<b>deposit_end_time:</b> {fmt_time(p.get('deposit_end_time'))}"
                 + (fmt_remaining(p.get('deposit_end_time'), now) if short_status == "DEPOSIT_PERIOD" else ""))
    lines.append(f"<b>voting_start_time:</b> {fmt_time(p.get('voting_start_time'))}")
    lines.append(f"<b>voting_end_time:</b> {fmt_time(p.get('voting_end_time'))}"
                 + (fmt_remaining(p.get('voting_end_time'), now) if short_status == "VOTING_PERIOD" else ""))
    if p.get("proposer"):
        lines.append(f"<b>proposer:</b> <code>{esc(p['proposer'])}</code>")
    lines.append("")
    lines.append(f"<b>summary:</b>\n{esc(truncate(p.get('summary', ''), SUMMARY_MAX_CHARS))}")
    lines.append("")
    lines.append(f'<a href="{MINTSCAN_URL.format(id=pid)}">Mintscan</a> | '
                 f'<a href="{PINGPUB_URL.format(id=pid)}">ping.pub</a>')
    msg = "\n".join(lines)
    if len(msg) > TELEGRAM_MAX_CHARS:  # should not happen with the truncation above
        msg = msg[: TELEGRAM_MAX_CHARS - 20] + "\n…(truncated)"
    return msg

# ---------------------------------------------------------------------------
# Fetching with failover
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    proposals: list[dict] | None = None
    endpoint: str | None = None
    errors: list[tuple[str, str]] = field(default_factory=list)

def fetch_proposals(endpoints: list[str], chain_id: str, timeout: float) -> FetchResult:
    result = FetchResult()
    for ep in endpoints:
        ep = ep.rstrip("/")
        try:
            info = http_get_json(ep + NODE_INFO_PATH, timeout)
            network = (info.get("default_node_info") or {}).get("network")
            if network != chain_id:
                raise RuntimeError(f"wrong chain id: expected {chain_id}, got {network!r}")
            data = http_get_json(ep + PROPOSALS_PATH, timeout)
            if "proposals" not in data:
                raise RuntimeError(f"unexpected response: {truncate(json.dumps(data), 200)}")
            result.proposals = data["proposals"] or []
            result.endpoint = ep
            log(f"OK   {ep} - {len(result.proposals)} recent proposals")
            return result
        except Exception as e:  # noqa: BLE001 - any failure → try next endpoint
            err = f"{type(e).__name__}: {truncate(str(e), 200)}"
            log(f"FAIL {ep} - {err}")
            result.errors.append((ep, err))
    return result

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

class Notifier:
    def __init__(self, token: str | None, chat_id: str | None, dry_run: bool, timeout: float):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run
        self.timeout = timeout

    def send(self, text: str, html_mode: bool = True, retries: int = 3) -> tuple[bool, str]:
        if self.dry_run:
            print("\n" + "=" * 70 + "\n" + text + "\n" + "=" * 70, flush=True)
            return True, "dry-run"
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
        if html_mode:
            payload["parse_mode"] = "HTML"
        last_err = ""
        for attempt in range(1, retries + 1):
            try:
                resp = http_post_json(url, payload, self.timeout)
                if resp.get("ok"):
                    return True, ""
                last_err = f"{resp.get('error_code')}: {resp.get('description')}"
                retry_after = (resp.get("parameters") or {}).get("retry_after")
                if retry_after:  # rate limited
                    time.sleep(min(int(retry_after) + 1, 60))
                    continue
                if resp.get("error_code") in (400, 401, 403, 404):
                    break  # bad request / bad token / bot not in channel - retrying won't help
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(2 * attempt)
        # Never leak the bot token into logs
        if self.token:
            last_err = last_err.replace(self.token, "***")
        return False, last_err

def write_notified_marker() -> None:
    """
    Tell the workflow's fallback step that already reported the error
    """
    path = os.getenv("NOTIFIED_MARKER_FILE")
    if path:
        try:
            with open(path, "w") as f:
                f.write("1")
        except OSError:
            pass

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_url() -> str:
    server = os.getenv("GITHUB_SERVER_URL")
    repo = os.getenv("GITHUB_REPOSITORY")
    run_id = os.getenv("GITHUB_RUN_ID")
    if server and repo and run_id:
        return f"{server}/{repo}/actions/runs/{run_id}"
    return ""

def main() -> int:
    dry_run = env_bool("DRY_RUN", False)
    timeout = float(os.getenv("REQUEST_TIMEOUT", "20"))
    chain_id = os.getenv("EXPECTED_CHAIN_ID", "cosmoshub-4").strip()
    statuses = {s.strip() for s in os.getenv("REPORT_STATUSES", DEFAULT_STATUSES).split(",") if s.strip()}
    notify_failover = env_bool("NOTIFY_ON_FAILOVER", True)
    endpoints_env = os.getenv("COSMOS_REST_ENDPOINTS", "").strip()
    endpoints = [e.strip() for e in endpoints_env.split(",") if e.strip()] or DEFAULT_ENDPOINTS

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not dry_run and (not token or not chat_id):
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use DRY_RUN=1).")
        return 1

    notifier = Notifier(token, chat_id, dry_run, timeout)
    link = run_url()
    footer = f"\nRun: {link}" if link else ""

    def report_error(text: str) -> None:
        ok, err = notifier.send(text, html_mode=False)
        if ok:
            write_notified_marker()
        else:
            log(f"ERROR: could not deliver error notice to Telegram: {err}")

    try:
        log(f"Checking {chain_id} via {len(endpoints)} endpoint(s); statuses={sorted(statuses)}")
        res = fetch_proposals(endpoints, chain_id, timeout)

        if res.proposals is None:
            details = "\n".join(f"- {ep}: {err}" for ep, err in res.errors)
            report_error(f"❌ Cosmos Hub proposal tracker: ALL endpoints failed - proposals NOT checked today.\n\n{details}{footer}")
            return 1

        if res.errors and notify_failover:
            details = "\n".join(f"- {ep}: {err}" for ep, err in res.errors)
            report_error(f"⚠️ Cosmos Hub proposal tracker: endpoint failover happened (data fetched from {res.endpoint}).\n\n{details}\n\nConsider updating the endpoint list.{footer}")

        active = [p for p in res.proposals if p.get("status") in statuses]
        if not active:
            log("No active proposals - nothing to post")
            return 0

        # Voting period first (most urgent), then by id ascending
        active.sort(key=lambda p: (p.get("status") != "PROPOSAL_STATUS_VOTING_PERIOD", int(p.get("id", 0))))
        now = datetime.now(timezone.utc)
        failures: list[tuple[str, str]] = []
        for p in active:
            pid = str(p.get("id", "?"))
            ok, err = notifier.send(build_message(p, now))
            if not ok:
                log(f"Telegram HTML send failed for #{pid}: {err} - retrying as plain text")
                # Fallback: same content without HTML formatting
                plain = html.unescape(re.sub(r"<[^>]+>", "", build_message(p, now)))
                plain += f"\n{MINTSCAN_URL.format(id=pid)}"
                ok, err2 = notifier.send(plain, html_mode=False)
                if not ok:
                    failures.append((pid, f"{err} / {err2}"))
            else:
                log(f"Posted proposal #{pid}")
            time.sleep(1)  # be gentle with Telegram rate limits

        if failures:
            details = "\n".join(f"- #{pid}: {err}" for pid, err in failures)
            report_error(f"❌ Cosmos Hub proposal tracker: failed to post {len(failures)} proposal(s) to Telegram.\n\n{details}{footer}")
            return 1

        log(f"Done - posted {len(active)} proposal(s)")
        return 0

    except Exception as e:  # noqa: BLE001 - last-resort guard
        log(f"UNEXPECTED ERROR: {type(e).__name__}: {e}")
        report_error(f"❌ Cosmos Hub proposal tracker crashed: {type(e).__name__}: {truncate(str(e), 500)}{footer}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
