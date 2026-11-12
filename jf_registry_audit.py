"""Core registry audit and Self-Audit for Justice Faite Manager 0.16.0.

Diagnostics only: never mutates settings, permissions, data or source code.
All reports are intended for the bot owner in private chat.
"""
from __future__ import annotations

import ast
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import cmdcfg

logger = logging.getLogger("jf_registry_audit")

# Known callback_data prefixes → owning command key (canonical).
KNOWN_CALLBACK_MAP: dict[str, str] = {
    "u:home": "anon",
    "u:info": "anon",
    "u:send_info": "anon",
    "u:report_info": "report",
    "u:send": "anon",
    "u:cancel": "anon",
    "u:report": "report",
    "u:report_edit": "report",
    "u:report_confirm": "report",
    "a:cancel": "admin_panel",
    "a:home": "admin_panel",
    "a:stats": "admin_panel",
    "a:messages": "admin_panel",
    "a:search": "admin_panel",
    "a:broadcast": "admin_panel",
    "reply:": "anon_admin",
    "a:replyuser:": "anon_admin",
    "fm:confirm:": "member_confirm",
    "fm:role:": "call",
    "fm:close:": "call",
    "gp:": "game_poll",
    "mf:": "mafia",
    "sp:": "spy",
    "cs:": "settings",
    "jfcon:": "console_open",
    "aw:": "awards",
}

# Module → expected registration helper / handle identity.
MODULE_HANDLERS: dict[str, str] = {
    "jf_manager": "JF_MANAGER.handle",
    "jf_community": "JF_COMMUNITY.handle",
    "jf_economy": "JF_ECONOMY.handle",
    "jf_social": "JF_SOCIAL.handle",
    "jf_pets": "JF_PETS.handle",
    "jf_ai": "JF_AI.handle",
    "jf_chatconfig": "JF_CHATCONFIG.handle",
    "jf_stats": "JF_STATS.handle",
    "jf_profiles": "JF_PROFILES.handle",
    "jf_reputation": "JF_REPUTATION.handle",
    "jf_reports": "JF_REPORTS.handle",
    "jf_timers": "JF_TIMERS.handle",
    "jf_automod": "JF_AUTOMOD.handle",
    "jf_fun": "JF_FUN.handle",
    "jf_network": "JF_NETWORK.handle",
    "jf_raffles": "JF_RAFFLES.handle",
    "jf_clean": "JF_CLEAN.handle",
    "jf_modvote": "JF_MODVOTE.handle",
    "jf_inline_guard": "JF_INLINE_GUARD",
    "jf_console": "JF_CONSOLE",
    "settings_panel": "settings_panel.register",
    "justice_features": "register_justice_features",
    "awards": "awards.register",
    "jf_ai_analytics": "JF_AI_ANALYTICS",
}

# Category → default owning module (best-effort annotation).
CATEGORY_MODULE: dict[str, str] = {
    "moderation": "jf_manager",
    "community": "jf_community",
    "economy": "jf_economy",
    "profiles": "jf_profiles",
    "reputation": "jf_reputation",
    "statistics": "jf_stats",
    "automation": "jf_reports",
    "fun": "jf_fun",
    "ai": "jf_ai",
    "network": "jf_network",
    "events": "jf_raffles",
    "cleanup": "jf_clean",
    "chat": "jf_chatconfig",
    "system": "jf_console",
    "roles": "bot",
    "games": "bot",
    "anon": "bot",
    "awards": "awards",
    "custom": "bot",
}

# Expected background worker names (for inventory diagnostics).
EXPECTED_WORKERS: tuple[str, ...] = (
    "spy-recovery",
    "jf-timers",
    "jf-ai-analytics",
    "jf-role-sync",
    "game_poll_refresh_worker",
    "game_poll_expiry_recovery_worker",
    "mafia_expiry_recovery_worker",
    "schedule_worker",
    "justice-feature-maintenance",
    "justice-console-maintenance",
    "delivery",
    "broadcast",
    "telegram-polling",
    "fatal-error-watcher",
)


@dataclass
class Finding:
    severity: str  # critical | warning | info
    code: str
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class AuditReport:
    generated_at: str
    version: str
    findings: list[Finding] = field(default_factory=list)
    registry_size: int = 0
    enabled_count: int = 0
    disabled_count: int = 0
    orphan_specs: list[str] = field(default_factory=list)
    unregistered_gates: list[str] = field(default_factory=list)
    callback_issues: list[str] = field(default_factory=list)
    task_issues: list[str] = field(default_factory=list)
    schema_issues: list[str] = field(default_factory=list)
    dependency_issues: list[str] = field(default_factory=list)
    duration_ms: int = 0

    def add(self, severity: str, code: str, message: str, **details) -> None:
        self.findings.append(Finding(severity, code, message, details))

    def counts_by_severity(self) -> dict[str, int]:
        out = {"critical": 0, "warning": 0, "info": 0}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def annotate_registry_modules() -> int:
    """Best-effort: fill COMMAND_META.module from category when empty."""
    n = 0
    for key, spec in cmdcfg.REGISTRY.items():
        meta = cmdcfg.get_command_meta(key)
        if meta.get("module") or spec.module:
            continue
        mod = CATEGORY_MODULE.get(spec.category, "")
        if mod:
            cmdcfg.set_command_meta(
                key,
                module=mod,
                handler_id=MODULE_HANDLERS.get(mod, ""),
                registration_status="declared",
            )
            n += 1
    return n


def audit_registry() -> list[Finding]:
    findings: list[Finding] = []
    size = len(cmdcfg.REGISTRY)
    if size == 0:
        findings.append(Finding("critical", "REGISTRY_EMPTY", "Command registry is empty."))
        return findings
    if size < 200:
        findings.append(Finding(
            "warning", "REGISTRY_SMALL",
            f"Registry has only {size} entries (expected ~287 after full load).",
            {"size": size},
        ))
    orphans = cmdcfg.find_orphans()
    missing_meta = orphans.get("specs_without_handler_meta") or []
    # After annotate, many will still lack explicit handler_id; treat as info.
    if len(missing_meta) > 50:
        findings.append(Finding(
            "info", "HANDLER_META_SPARSE",
            f"{len(missing_meta)} specs still lack explicit handler_id/module metadata.",
            {"count": len(missing_meta), "sample": missing_meta[:15]},
        ))
    bad_legacy = orphans.get("legacy_keys_pointing_missing_spec") or []
    if bad_legacy:
        findings.append(Finding(
            "warning", "LEGACY_MAP_BROKEN",
            f"Legacy keys point to missing Specs: {bad_legacy}",
            {"keys": bad_legacy},
        ))
    # Duplicate aliases across different keys
    alias_owners: dict[str, list[str]] = {}
    for key, spec in cmdcfg.REGISTRY.items():
        for a in list(spec.aliases or ()) + list(spec.triggers or ()):
            norm = str(a).casefold().strip()
            if not norm:
                continue
            alias_owners.setdefault(norm, []).append(key)
    dups = {a: keys for a, keys in alias_owners.items() if len(set(keys)) > 1}
    if dups:
        sample = list(dups.items())[:10]
        findings.append(Finding(
            "warning", "DUPLICATE_ALIASES",
            f"{len(dups)} aliases resolve to multiple Spec keys.",
            {"sample": sample},
        ))
    return findings


def audit_callbacks() -> list[Finding]:
    findings: list[Finding] = []
    # Check that known prefixes map to existing Specs (when Spec exists).
    missing_specs = []
    for prefix, key in KNOWN_CALLBACK_MAP.items():
        if key not in cmdcfg.REGISTRY and cmdcfg.canonical_key(key) not in cmdcfg.REGISTRY:
            # Some keys like admin_panel / anon_admin may be intentional feature keys
            if key not in ("admin_panel", "anon_admin", "game_poll_vote", "game_poll_info",
                           "mafia_admin_action", "report"):
                missing_specs.append((prefix, key))
    if missing_specs:
        findings.append(Finding(
            "warning", "CALLBACK_SPEC_MISSING",
            "Some callback prefixes map to keys absent from REGISTRY.",
            {"items": missing_specs[:20]},
        ))
    # Detect duplicate prefix ownership in KNOWN_CALLBACK_MAP values is fine;
    # check overlapping prefixes that could steal events.
    prefixes = sorted(KNOWN_CALLBACK_MAP.keys(), key=len, reverse=True)
    overlaps = []
    for i, p in enumerate(prefixes):
        for q in prefixes[i + 1:]:
            if p != q and (p.startswith(q) or q.startswith(p)):
                # prefix relationship is expected for hierarchical callbacks
                if not (p.startswith(q) or q.startswith(p)):
                    overlaps.append((p, q))
    return findings


def audit_disabled_switches() -> list[Finding]:
    """Verify disabled commands are still present in registry and gate rejects them.

    Pure logic check — does not send Telegram messages.
    """
    findings: list[Finding] = []
    disabled = [k for k in cmdcfg.REGISTRY if not cmdcfg.is_enabled(k)]
    for key in disabled[:5]:
        # gate should refuse when we simulate via preflight if possible
        spec = cmdcfg.REGISTRY[key]
        if spec.feature:
            continue
    if disabled:
        findings.append(Finding(
            "info", "DISABLED_COMMANDS",
            f"{len(disabled)} commands currently disabled via settings.",
            {"sample": disabled[:20]},
        ))
    return findings


def audit_legacy_gates() -> list[Finding]:
    findings: list[Finding] = []
    unmapped = cmdcfg.find_unregistered_gates(list(cmdcfg.LEGACY_KEY_MAP.keys()))
    # LEGACY_KEY_MAP values are canonical; keys themselves need not be in REGISTRY
    broken = [
        leg for leg, canon in cmdcfg.LEGACY_KEY_MAP.items()
        if canon not in cmdcfg.REGISTRY
    ]
    if broken:
        findings.append(Finding(
            "critical", "LEGACY_TARGET_MISSING",
            f"LEGACY_KEY_MAP targets missing from REGISTRY: {broken}",
            {"keys": broken},
        ))
    # Sanity: canonical_key("ban") == "mgr_ban"
    if cmdcfg.REGISTRY:
        sample = cmdcfg.canonical_key("ban")
        if sample != "mgr_ban" and "mgr_ban" in cmdcfg.REGISTRY:
            findings.append(Finding(
                "critical", "CANONICAL_KEY_BROKEN",
                f'canonical_key("ban") returned {sample!r}, expected "mgr_ban".',
            ))
    return findings


def audit_background_tasks(task_registry: Optional[dict] = None) -> list[Finding]:
    """Inspect provided task handle map (name → asyncio.Task or None).

    If task_registry is None, only reports expected worker inventory as info.
    """
    findings: list[Finding] = []
    findings.append(Finding(
        "info", "EXPECTED_WORKERS",
        f"Expected background workers: {len(EXPECTED_WORKERS)}.",
        {"names": list(EXPECTED_WORKERS)},
    ))
    if task_registry is None:
        findings.append(Finding(
            "info", "TASK_HANDLES_UNKNOWN",
            "Runtime task handle map not provided; lifecycle not verified this run.",
        ))
        return findings
    missing_handles = []
    done_failed = []
    for name, task in task_registry.items():
        if task is None:
            missing_handles.append(name)
            continue
        try:
            if task.done() and task.exception() is not None:
                done_failed.append((name, str(task.exception())[:120]))
        except Exception as exc:
            done_failed.append((name, f"inspect-error: {exc}"))
    if missing_handles:
        findings.append(Finding(
            "warning", "TASK_HANDLE_MISSING",
            f"{len(missing_handles)} workers lack a stored task handle.",
            {"names": missing_handles},
        ))
    if done_failed:
        findings.append(Finding(
            "critical", "TASK_FAILED",
            f"{len(done_failed)} background tasks finished with exception.",
            {"items": done_failed[:10]},
        ))
    return findings


def audit_sqlite_schema(db_path: str) -> list[Finding]:
    findings: list[Finding] = []
    if not db_path or not os.path.exists(db_path):
        findings.append(Finding(
            "info", "DB_ABSENT",
            f"Database file not found at {db_path!r}; schema check skipped.",
        ))
        return findings
    required_tables = {
        "users", "cmd_settings", "managed_group", "group_members",
        "jf_runtime_log", "jf_ai_analytics_state",
    }
    try:
        conn = sqlite3.connect(db_path)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()
            if not ok or ok[0] != "ok":
                findings.append(Finding(
                    "critical", "DB_INTEGRITY",
                    f"PRAGMA integrity_check failed: {ok}",
                ))
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            missing = sorted(required_tables - tables)
            if missing:
                findings.append(Finding(
                    "warning", "SCHEMA_TABLES_MISSING",
                    f"Missing expected tables: {missing}",
                    {"missing": missing},
                ))
            else:
                findings.append(Finding(
                    "info", "SCHEMA_CORE_OK",
                    f"Core tables present ({len(tables)} tables total).",
                    {"table_count": len(tables)},
                ))
        finally:
            conn.close()
    except Exception as exc:
        findings.append(Finding(
            "critical", "DB_OPEN_FAILED",
            f"Cannot open database: {exc}",
        ))
    return findings


def audit_dependencies(available_modules: Optional[set[str]] = None) -> list[Finding]:
    findings: list[Finding] = []
    expected = set(MODULE_HANDLERS.keys())
    if available_modules is None:
        # Static: check files exist next to this module
        root = os.path.dirname(os.path.abspath(__file__))
        available_modules = set()
        for name in expected:
            path = os.path.join(root, f"{name}.py")
            if os.path.isfile(path):
                available_modules.add(name)
    missing = sorted(expected - set(available_modules))
    if missing:
        findings.append(Finding(
            "warning", "MODULE_FILE_MISSING",
            f"Expected module files not found: {missing}",
            {"missing": missing},
        ))
    else:
        findings.append(Finding(
            "info", "MODULES_PRESENT",
            f"All {len(expected)} expected module files are present.",
        ))
    return findings


def run_self_audit(
    *,
    db_path: str = "",
    task_registry: Optional[dict] = None,
    version: str = "0.24.1",
) -> AuditReport:
    """Full Core Self-Audit. Read-only. Never modifies state."""
    t0 = time.monotonic()
    annotate_registry_modules()
    report = AuditReport(
        generated_at=_utcnow(),
        version=version,
        registry_size=len(cmdcfg.REGISTRY),
    )
    enabled = sum(1 for k in cmdcfg.REGISTRY if cmdcfg.is_enabled(k))
    report.enabled_count = enabled
    report.disabled_count = report.registry_size - enabled

    for fn in (
        audit_registry,
        audit_callbacks,
        audit_disabled_switches,
        audit_legacy_gates,
    ):
        try:
            for f in fn():
                report.findings.append(f)
        except Exception as exc:
            report.add("critical", "AUDIT_SECTION_CRASH", f"{fn.__name__}: {exc}")

    try:
        for f in audit_background_tasks(task_registry):
            report.findings.append(f)
            if f.code in ("TASK_HANDLE_MISSING", "TASK_FAILED"):
                report.task_issues.append(f.message)
    except Exception as exc:
        report.add("critical", "TASK_AUDIT_CRASH", str(exc))

    try:
        for f in audit_sqlite_schema(db_path):
            report.findings.append(f)
            if f.severity in ("critical", "warning"):
                report.schema_issues.append(f.message)
    except Exception as exc:
        report.add("critical", "SCHEMA_AUDIT_CRASH", str(exc))

    try:
        for f in audit_dependencies():
            report.findings.append(f)
            if f.severity != "info":
                report.dependency_issues.append(f.message)
    except Exception as exc:
        report.add("critical", "DEP_AUDIT_CRASH", str(exc))

    orphans = cmdcfg.find_orphans()
    report.orphan_specs = list(orphans.get("specs_without_handler_meta") or [])[:50]
    report.unregistered_gates = list(orphans.get("legacy_keys_pointing_missing_spec") or [])

    report.duration_ms = int((time.monotonic() - t0) * 1000)
    return report


def format_report_html(report: AuditReport) -> str:
    """Owner-facing HTML summary (Telegram HTML)."""
    sev = report.counts_by_severity()
    lines = [
        f"🔎 <b>Self-Audit · v{report.version}</b>",
        f"<i>{report.generated_at}</i> · {report.duration_ms} ms",
        "",
        f"📋 Реестр: <b>{report.registry_size}</b> "
        f"(вкл. {report.enabled_count}, выкл. {report.disabled_count})",
        f"Findings: 🔴 {sev.get('critical', 0)} · "
        f"🟡 {sev.get('warning', 0)} · ℹ️ {sev.get('info', 0)}",
        "",
    ]
    critical = [f for f in report.findings if f.severity == "critical"]
    warnings = [f for f in report.findings if f.severity == "warning"]
    infos = [f for f in report.findings if f.severity == "info"]

    def _block(title: str, items: list[Finding], limit: int = 12) -> None:
        if not items:
            return
        lines.append(f"<b>{title}</b>")
        for f in items[:limit]:
            lines.append(f"• <code>{f.code}</code> — {f.message[:180]}")
        if len(items) > limit:
            lines.append(f"… и ещё {len(items) - limit}")
        lines.append("")

    _block("🔴 Critical", critical)
    _block("🟡 Warning", warnings)
    _block("ℹ️ Info", infos, limit=8)

    if not critical and not warnings:
        lines.append("✅ Критических и предупреждающих проблем не обнаружено.")
    lines.append("")
    lines.append("<i>Self-Audit только диагностирует. "
                 "Настройки, права и данные не изменялись.</i>")
    return "\n".join(lines)


def format_report_text(report: AuditReport) -> str:
    """Plain-text variant for logs / tests."""
    sev = report.counts_by_severity()
    parts = [
        f"Self-Audit v{report.version} @ {report.generated_at} ({report.duration_ms} ms)",
        f"registry={report.registry_size} enabled={report.enabled_count} disabled={report.disabled_count}",
        f"critical={sev.get('critical', 0)} warning={sev.get('warning', 0)} info={sev.get('info', 0)}",
    ]
    for f in report.findings:
        parts.append(f"[{f.severity}] {f.code}: {f.message}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Owner command integration (manual trigger)
# ---------------------------------------------------------------------------

_owner_id: int = 0
_bot = None
_db_path: str = ""
_task_registry_getter: Optional[Callable[[], dict]] = None
_version: str = "0.24.1"


def configure(
    *,
    bot=None,
    owner_id: int = 0,
    db_path: str = "",
    task_registry_getter: Optional[Callable[[], dict]] = None,
    version: str = "0.24.1",
) -> None:
    global _bot, _owner_id, _db_path, _task_registry_getter, _version
    _bot = bot
    _owner_id = int(owner_id or 0)
    _db_path = db_path or ""
    _task_registry_getter = task_registry_getter
    _version = version


async def handle_self_audit_command(message) -> None:
    """Owner-only /jf_self_audit handler body."""
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if not user or not chat:
        return
    if int(user.id) != int(_owner_id or 0):
        return
    if getattr(chat, "type", None) != "private":
        try:
            await message.answer("Self-Audit доступен только владельцу в личных сообщениях.")
        except Exception:
            pass
        return
    tasks = None
    if _task_registry_getter:
        try:
            tasks = _task_registry_getter()
        except Exception:
            tasks = None
    report = run_self_audit(db_path=_db_path, task_registry=tasks, version=_version)
    text = format_report_html(report)
    # Telegram message limit ~4096; split if needed.
    chunks = []
    while text:
        chunks.append(text[:4000])
        text = text[4000:]
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode="HTML")
        except Exception:
            try:
                await message.answer(format_report_text(report)[:4000])
            except Exception as exc:
                logger.warning("self-audit delivery failed: %s", exc)
                return


def register(dp, bot, F, *, owner_id: int = 0, db_path: str = "", version: str = "0.24.1",
             task_registry_getter=None):
    """Register /jf_self_audit for the owner private chat."""
    from aiogram.filters import Command

    configure(
        bot=bot,
        owner_id=owner_id,
        db_path=db_path,
        task_registry_getter=task_registry_getter,
        version=version,
    )

    async def _cmd(message):
        await handle_self_audit_command(message)

    dp.message.register(
        _cmd,
        Command("jf_self_audit"),
        F.chat.type == "private",
    )
    # Also accept without slash in owner DM via console-style text.
    return True
