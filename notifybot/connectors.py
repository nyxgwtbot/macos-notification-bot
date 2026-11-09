"""Connectors layer (v1): the server-side executor of the agent's fifth tool,
``native.call`` (native macOS apps via AppleScript).

Injection-safety decision (MANDATORY, approved): every model-supplied string
travels ONLY as a positional argv item to a CONSTANT AppleScript source::

    subprocess.run(["osascript", "-e", SCRIPT_CONST, *args, ...])

The script source for each (domain, action) is a frozen constant below whose
single input channel is ``on run argv`` — user bytes NEVER appear in script
source (no f-strings, no shell, no ``run script`` of dynamic text). List
outputs are joined in-script with ``ASCII character 31`` and split here —
never comma-split.

Consent model (TCC): reminders/notes/calendar/music/mail/contacts/messages
actions require ``granted`` state, which is produced by ``probe()`` (fired from Settings ›
Connectors) or self-healed by a successful action. Actions that come back
denied persist ``denied`` and raise the PT-BR consent error. ``system`` and
``shortcuts`` (system CLI) are ungated (no consent). Probes are single-flight
per domain; concurrent probes raise ``ProbeBusy`` (API maps to 409).

Deferred/later domains table (findmy still deferred; contacts/messages are
implemented in v2 and ARE rows of GET /api/connectors; zero secrets involved;
PII/read-sensitive):

==============  =========================  ==================================
Domain          Example actions            Why deferred to v2
==============  =========================  ==================================
mail            unread/mailboxes/recent    implemented in v1 (read-level-1)
contacts        search/get/create          implemented in v2 (same consent gate; no delete/update)
messages        send_message/thread-dates  implemented in v2 (send-only; date-only threads)
findmy          locate devices             location + privacy escalation
==============  =========================  ==================================

Outcome classification (actions and probes share it):
- exit 0                                              -> ok (probe: granted)
- subprocess.TimeoutExpired                           -> probe: needs_consent
                                                        action: PT-BR timeout error
   (state split: an ALREADY-granted domain gets first-use/sync retry wording
    with NO Settings/Connect redirect; ungranted states get the consent hint —
    a hanging TCC consent prompt is the common cause there)
- stderr contains -1743 / "Not authorized"            -> denied (persisted;
                                                        action: PT-BR error)
- stderr contains the no-account marker               -> action: PT-BR error
  (NOTES_CREATE raises "connectors-no-notes-account")    account setup first
- any other non-zero exit                             -> probe: denied
                                                        action: error w/ trimmed
                                                        stderr (English)

Mock hook: with env ``NOTIFYBOT_CONNECTORS_MOCK=1`` the runner seam executes a
per-process in-memory world (reminders lists, notes, calendar events, music
state, volume, front app, toasts, shortcut names, mail unread/mailbox/recent
fixtures, contacts people, message thread dates, finder Desktop + Downloads
virtual-FS fixtures, quicktime fake doc ids "QT-MOCK-<n>" + fake saved paths)
so smokes exercise add->list observability and the endpoints WITHOUT
osascript.
"""

import json
import os
import re
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from notifybot.agent_tools import _write_atomic

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Domains exposed by native.call in v1 (order == GET /api/connectors row order).
CONNECTOR_DOMAINS: Tuple[str, ...] = (
    "reminders", "notes", "calendar", "music", "shortcuts", "mail",
    "contacts", "messages", "finder", "quicktime", "system"
)

#: Whitelist: the only (domain, action) pairs dispatch accepts.
CONNECTOR_ACTIONS: Dict[str, Tuple[str, ...]] = {
    "reminders": ("list_lists", "list_reminders", "add", "complete"),
    "notes": ("search", "read", "create"),
    "calendar": ("list_calendars", "list_events", "add_event", "delete_event"),
    "music": ("status", "play", "pause", "next", "previous", "play_playlist", "search_store", "volume"),
    "shortcuts": ("list", "run"),
    "mail": ("unread_count", "mailboxes", "list_recent"),
    "contacts": ("search", "get", "create"),
    "messages": ("send_message", "list_recent_threads"),
    "finder": ("list", "reveal", "info", "move", "trash"),
    "quicktime": ("start_recording", "stop_recording", "new_audio_recording", "play"),
    "system": ("volume", "front_app", "open", "toast"),
}

#: Domains that require granted consent state before ANY action runs
#: (AppleScript automation of these apps triggers macOS TCC consent).
GATED_DOMAINS: Tuple[str, ...] = ("reminders", "notes", "calendar", "music", "mail", "contacts", "messages", "finder", "quicktime")

#: Hard per-call timeout (also enforced by subprocess.run's timeout kwarg).
CONNECTORS_TIMEOUT_S = 8.0

#: Shortcuts CLI can cold-start slower than osascript (mission: 10s).
SHORTCUTS_TIMEOUT_S = 10.0

#: QuickTime recording: `new screen recording` opens a setup window and the
#: scripts busy-wait up to 30s for the document, so the runner needs a
#: window that covers that wait (45s: wait loop + activate + safety margin).
QUICKTIME_RECORD_TIMEOUT_S = 45.0

#: Script returns this marker when the recording doc never appeared (60s
#: wait exhausted): the setup window is awaiting a manual confirmation.
QUICKTIME_DOC_TIMEOUT_MARKER = "connectors-qt-doc-timeout"

#: User-visible error for the doc-timeout marker (honest: setup window may
#: need a manual click; script side holds NO slot in that case).
ERR_QT_RECORD_SETUP = (
    "{display} não conseguiu iniciar a gravação — a tela de preparação "
    "provavelmente pede uma confirmação manual. Nada foi gravado."
)

#: Env var that swaps the osascript runner for the in-process mock world.
CONNECTORS_MOCK_ENV = "NOTIFYBOT_CONNECTORS_MOCK"

# Server-side validation caps.
TITLE_MAX_CHARS = 512
BODY_MAX_BYTES = 16384
READ_MAX_BYTES = 16384
LIST_LIMIT_MAX = 50
LIST_LIMIT_DEFAULT = 20
LIST_NAME_MAX_CHARS = 256
QUERY_MAX_CHARS = 256
DATERANGE_MAX_DAYS = 30
DATERANGE_DEFAULT_DAYS = 7

#: Shortcuts bridge caps: run-stdout tail (bytes) and piped input text (chars).
SHORTCUTS_OUTPUT_MAX_BYTES = 4096
SHORTCUTS_INPUT_MAX_CHARS = 512

#: Mail list_recent cap: 1..20 rows (above the cap ERRORS — _as_days precedent).
MAIL_LIST_MAX_COUNT = 20

#: Contacts caps: search limit default 10, cap 50 (LIST_LIMIT_MAX, above
#: the cap ERRORS — _as_days precedent); emails/phones max 3 each;
#: retrieved note bytes capped like a note body (16 KB).
CONTACTS_LIST_LIMIT_DEFAULT = 10
CONTACTS_EMAILS_MAX_COUNT = 3
CONTACTS_PHONES_MAX_COUNT = 3
CONTACTS_NOTE_MAX_BYTES = BODY_MAX_BYTES

#: Messages caps: recipient 256 chars, text 1024 chars, thread rows
#: count 1..10 (default == cap, above the cap ERRORS — mail precedent).
MESSAGES_TO_MAX_CHARS = 256
MESSAGES_TEXT_MAX_CHARS = 1024
MESSAGES_THREADS_MAX_COUNT = 10

#: Finder caps: path inputs 512 chars (SHORTCUTS_INPUT_MAX_CHARS precedent);
#: list rows 1..100 (hard trim — LIST_LIMIT_MAX family).
FINDER_PATH_MAX_CHARS = 512
FINDER_LIST_MAX_ITEMS = 100

#: QuickTime caps: file/path inputs 512 chars.
QUICKTIME_FILE_MAX_CHARS = 512

#: Finder allowed roots: model-suggested paths must resolve under ONE of these
#: (expanduser + realpath, then prefix-compare — no "~" expansion tricks).
_FINDER_ALLOWED_ROOTS: Tuple[str, ...] = (
    "~/Desktop", "~/Documents", "~/Downloads", "~/apps",
)

#: In-script list delimiter: ASCII character 31 (unit separator). AppleScript
#: joins list results with it; Python splits on the same byte below.
_DELIM = "\x1f"

STATUS_UNKNOWN = "unknown"
STATUS_GRANTED = "granted"
STATUS_NEEDS_CONSENT = "needs_consent"
STATUS_DENIED = "denied"
VALID_STATUSES = frozenset({STATUS_UNKNOWN, STATUS_GRANTED, STATUS_NEEDS_CONSENT, STATUS_DENIED})

# English hint strings surfaced by GET /api/connectors (Settings copy is
# English per owner rule; only chat-relayed tool errors are PT-BR).
HINT_SYSTEM = "no consent required"
HINT_GRANTED = "connected — the in-app agent may use this domain."
HINT_NEEDS_CONSENT = "macOS may show a system consent prompt — approve it while this app is focused."
HINT_DENIED = (
    "macOS previously denied automation for this domain — press Connect and "
    "approve the prompt while this app is focused."
)
HINT_UNKNOWN = HINT_NEEDS_CONSENT
HINT_SHORTCUTS = (
    "Shortcuts run through the system CLI — no consent needed. A shortcut may "
    "touch apps that ask for their own permission."
)

# `open` target validation (system.open runs ["open", ...], never osascript).
_URL_TARGET_RE = re.compile(r"^https?://")
_APP_TARGET_RE = re.compile(r"^[A-Za-z0-9 ._-]{1,64}$")

# English (ToolFailure-style) whitelist/validation error strings.
ERR_UNKNOWN_DOMAIN = "unknown domain: {domain!r} (valid: reminders, notes, calendar, music, shortcuts, mail, contacts, messages, finder, quicktime, system)"
ERR_UNKNOWN_ACTION = "unknown action {action!r} for domain {domain!r} (valid: {actions})"
ERR_PARAMS_DICT = "params must be a JSON object"

# Display names used inside PT-BR user-facing error strings.
_DISPLAY_NAMES: Dict[str, str] = {
    "reminders": "Reminders", "notes": "Notes", "calendar": "Calendar",
    "music": "Music", "shortcuts": "Shortcuts", "mail": "Mail",
    "contacts": "Contacts", "messages": "Messages", "finder": "Finder", "quicktime": "QuickTime",
    "system": "System",
}

# Structured in-script error marker: NOTES_CREATE raises this AppleScript error
# literal when no Notes account exists (count of accounts is 0); the Python
# classifier maps it to the PT-BR "set up an account first" tool error. The
# literal lives ONLY in the frozen script + this constant (kept in sync).
NO_ACCOUNT_MARKER = "connectors-no-notes-account"

# Mail structured in-script markers: the MAIL_* scripts RETURN these literals
# on rc-0 stdout (mirroring the "not_found" stdout convention above — NOT the
# stderr route NOTES_CREATE takes), so they are parsed on the success path.
# The Python parsers (see _raise_if_mail_marker) map them to the clean
# user-facing copies; the literals live ONLY in the frozen scripts + these
# constants (kept in sync).
MAIL_MISSING_MAILBOX_MARKER = "connectors-mail-missing-mailbox"
MAIL_ERROR_MARKER = "connectors-mail-error"

# Contacts/Messages structured in-script markers (rc-0 stdout route,
# equality-after-strip — mirrored mail convention): returned by the
# frozen CONTACTS_GET script when the exact name matched nobody, and by
# MESSAGES_SEND_CONTACT when the recipient matched no contact, matched
# MORE than one contact (ambiguous — refuse, never guess a recipient),
# or resolve produced no sendable handle. Literals live ONLY in the frozen
# scripts + these constants (kept in sync).
CONTACTS_NOT_FOUND_MARKER = "connectors-contacts-not-found"
MESSAGES_UNKNOWN_RECIPIENT_MARKER = "connectors-messages-unknown-recipient"

# English (ToolFailure-style) refusal copy for the messages send guard.
ERR_UNKNOWN_RECIPIENT = (
    "unknown recipient: {to!r} is not a known contact, e-mail address, "
    "or +E.164 phone"
)

# Finder structured in-script markers (rc-0 stdout route, equality-after-strip
# — mirror contacts/messages convention). Literals live ONLY in the frozen
# scripts + these constants (kept in sync).
FINDER_NOT_FOUND_MARKER = "connectors-finder-not-found"
FINDER_DST_DIR_MISSING_MARKER = "connectors-finder-dst-dir-missing"
FINDER_DST_EXISTS_MARKER = "connectors-finder-dst-exists"

# QuickTime structured in-script marker: stop_recording may find the remembered
# document gone (closed externally) — rc-0 stdout refusal route.
QUICKTIME_DOC_GONE_MARKER = "connectors-qt-doc-gone"

# English (ToolFailure-style) refusal copies: finder + quicktime validation.
ERR_FINDER_NOT_FOUND = "path not found: {path}"
ERR_FINDER_NO_FOLDER = "no folder at path: {path}"
ERR_FINDER_DST_DIR_MISSING = "destination folder not found: {dir}"
ERR_FINDER_DST_EXISTS = (
    "destination already contains an item named {name!r} — refusing to overwrite"
)
ERR_PATH_OUTSIDE_ROOTS = (
    "path {path!r} is outside the allowed folders: "
    "~/Desktop, ~/Documents, ~/Downloads, ~/apps"
)
ERR_TRASH_HOME_REFUSED = "refusing to move the home folder to the Trash: {path!r}"
ERR_NO_RECORDING = "no QuickTime recording is in progress (start one first)"
ERR_QT_ALREADY_RECORDING = (
    "a QuickTime recording is already in progress (document {doc_id}) — stop it first"
)
ERR_QT_DOC_GONE = (
    "the QuickTime recording (document {doc_id}) is no longer available — try again"
)
ERR_QT_PLAY_FILE = "path is not an existing file: {path}"

# ---------------------------------------------------------------------------
# Static AppleScript sources — FROZEN constants; the ONLY input channel is
# `on run argv` (argv items are plain strings bound at run time).
# ---------------------------------------------------------------------------

REMINDERS_LIST_LISTS = """
on run argv
	set _names to {}
	tell application id "com.apple.reminders"
		set _names to name of every list
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

REMINDERS_LIST_REMINDERS = """
on run argv
	set _listName to (item 1 of argv) as text
	set _maxCount to (item 2 of argv) as integer
	set _names to {}
	tell application id "com.apple.reminders"
		set _names to (name of reminders of list _listName whose completed is false)
	end tell
	if _maxCount is less than (count of _names) then
		set _names to items 1 through _maxCount of _names
	end if
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

REMINDERS_ADD = """
on run argv
	set _listName to (item 1 of argv) as text
	set _title to (item 2 of argv) as text
	set _yearText to (item 3 of argv) as text
	set _monthText to (item 4 of argv) as text
	set _dayText to (item 5 of argv) as text
	tell application id "com.apple.reminders"
		if _yearText is "" then
			tell list _listName
				make new reminder with properties {name:_title}
			end tell
		else
			-- Date is built component-wise from integer argv parts: parsing a
			-- date STRING ("2026-10-28") is locale-dependent; integers are not.
			set _due to current date
			set day of _due to 1
			set year of _due to (_yearText as integer)
			set month of _due to (_monthText as integer)
			set day of _due to (_dayText as integer)
			set hours of _due to 12
			set minutes of _due to 0
			set seconds of _due to 0
			tell list _listName
				make new reminder with properties {name:_title, due date:_due}
			end tell
		end if
	end tell
	return "ok"
end run
"""

REMINDERS_COMPLETE = """
on run argv
	set _listName to (item 1 of argv) as text
	set _name to (item 2 of argv) as text
	tell application id "com.apple.reminders"
		set _matches to (reminders of list _listName whose completed is false and name is _name)
		if (count of _matches) is 0 then
			return "not_found"
		end if
		set completed of item 1 of _matches to true
		return "ok" & (ASCII character 31) & (name of item 1 of _matches)
	end tell
end run
"""

NOTES_SEARCH = """
on run argv
	set _query to (item 1 of argv) as text
	set _maxCount to (item 2 of argv) as integer
	set _names to {}
	tell application id "com.apple.Notes"
		set _names to (name of notes of account 1 whose name contains _query)
	end tell
	if _maxCount is less than (count of _names) then
		set _names to items 1 through _maxCount of _names
	end if
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

NOTES_READ = """
on run argv
	set _name to (item 1 of argv) as text
	tell application id "com.apple.Notes"
		set _matches to (notes of account 1 whose name is _name)
		if (count of _matches) is 0 then
			return "not_found"
		end if
		return (body of item 1 of _matches)
	end tell
end run
"""

NOTES_CREATE = """
on run argv
	set _title to (item 1 of argv) as text
	set _body to (item 2 of argv) as text
	tell application id "com.apple.Notes"
		if (count of accounts) is 0 then
			error "connectors-no-notes-account"
		end if
		if _body is "" then
			make new note at folder 1 of account 1 with properties {name:_title, body:_title}
		else
			make new note at folder 1 of account 1 with properties {name:_title, body:_body}
		end if
	end tell
	return "ok"
end run
"""

SYSTEM_VOLUME_GET = """
on run argv
	return ((output volume of (get volume settings)) as text)
end run
"""

SYSTEM_VOLUME_SET = """
on run argv
	set _level to (item 1 of argv) as integer
	set volume output volume _level
	return ((output volume of (get volume settings)) as text)
end run
"""

SYSTEM_FRONT_APP = """
on run argv
	tell application id "com.apple.systemevents"
		return (name of first process whose frontmost is true)
	end tell
end run
"""

SYSTEM_TOAST = """
on run argv
	set _title to (item 1 of argv) as text
	set _body to (item 2 of argv) as text
	display notification _body with title _title
	return "ok"
end run
"""

CALENDAR_LIST_CALENDARS = """
on run argv
	set _names to {}
	tell application id "com.apple.iCal"
		set _names to name of every calendar
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

CALENDAR_LIST_EVENTS = """
on run argv
	set _calName to (item 1 of argv) as text
	set _days to (item 2 of argv) as integer
	set _now to current date
	set _horizon to _now + (_days * days)
	set _rows to {}
	tell application id "com.apple.iCal"
		repeat with _cal in calendars
			if _calName is "" or (name of _cal is _calName) then
				repeat with _ev in (every event of _cal whose start date is greater than or equal to _now and start date is less than or equal to _horizon)
					set end of _rows to (summary of _ev)
					set end of _rows to (my _fmtDate(start date of _ev))
					set end of _rows to (my _fmtDate(end date of _ev))
				end repeat
			end if
		end repeat
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_rows as text)
end run

on _fmtDate(_d)
	set _m to (month of _d as integer) as text
	set _dd to (day of _d) as text
	set _h to (hours of _d) as text
	set _mi to (minutes of _d) as text
	if length of _m is 1 then set _m to "0" & _m
	if length of _dd is 1 then set _dd to "0" & _dd
	if length of _h is 1 then set _h to "0" & _h
	if length of _mi is 1 then set _mi to "0" & _mi
	return ((year of _d) as text) & "-" & _m & "-" & _dd & "T" & _h & ":" & _mi
end _fmtDate
"""

CALENDAR_ADD_EVENT = """
on run argv
	set _summary to (item 1 of argv) as text
	set _calName to (item 12 of argv) as text
	set _notes to (item 13 of argv) as text
	set _alertText to (item 14 of argv) as text
	set _start to my _buildDate(item 2 of argv, item 3 of argv, item 4 of argv, item 5 of argv, item 6 of argv)
	set _end to my _buildDate(item 7 of argv, item 8 of argv, item 9 of argv, item 10 of argv, item 11 of argv)
	tell application id "com.apple.iCal"
		if _calName is "" then
			try
				set _targetCal to first calendar whose writable is true
			on error
				set _targetCal to calendar 1
			end try
		else
			set _targetCal to calendar _calName
		end if
		tell _targetCal
			set _ev to make new event with properties {summary:_summary, start date:_start, end date:_end}
		end tell
		if _notes is not "" then
			try
				set description of _ev to _notes
			end try
		end if
		if _alertText is not "" then
			try
				tell _ev
					make new sound alarm at end of alarms with properties {trigger interval:0 - (_alertText as integer)}
				end tell
			end try
		end if
	end tell
	return _summary & (ASCII character 31) & (my _fmtDate(_start))
end run

on _buildDate(_y, _m, _d, _h, _mi)
	set _dt to current date
	set day of _dt to 1
	set year of _dt to (_y as integer)
	set month of _dt to (_m as integer)
	set day of _dt to (_d as integer)
	set hours of _dt to (_h as integer)
	set minutes of _dt to (_mi as integer)
	set seconds of _dt to 0
	return _dt
end _buildDate

on _fmtDate(_d)
	set _m to (month of _d as integer) as text
	set _dd to (day of _d) as text
	set _h to (hours of _d) as text
	set _mi to (minutes of _d) as text
	if length of _m is 1 then set _m to "0" & _m
	if length of _dd is 1 then set _dd to "0" & _dd
	if length of _h is 1 then set _h to "0" & _h
	if length of _mi is 1 then set _mi to "0" & _mi
	return ((year of _d) as text) & "-" & _m & "-" & _dd & "T" & _h & ":" & _mi
end _fmtDate
"""

CALENDAR_DELETE_EVENT = """
on run argv
	set _summary to (item 1 of argv) as text
	set _calName to (item 5 of argv) as text
	set _fromText to (item 2 of argv) as text
	set _count to 0
	tell application id "com.apple.iCal"
		if _calName is not "" then
			set _calList to {calendar _calName}
		else
			set _calList to every calendar
		end if
		repeat with _cal in _calList
			if _fromText is "" then
				set _matches to (every event of _cal whose summary is _summary)
			else
				set _from to my _buildDayStart(item 2 of argv, item 3 of argv, item 4 of argv)
				set _matches to (every event of _cal whose summary is _summary and start date is greater than or equal to _from)
			end if
			repeat with _i from (count of _matches) to 1 by -1
				delete item _i of _matches
				set _count to _count + 1
			end repeat
		end repeat
	end tell
	return (_count as text)
end run

on _buildDayStart(_y, _m, _d)
	set _dt to current date
	set day of _dt to 1
	set year of _dt to (_y as integer)
	set month of _dt to (_m as integer)
	set day of _dt to (_d as integer)
	set hours of _dt to 0
	set minutes of _dt to 0
	set seconds of _dt to 0
	return _dt
end _buildDayStart
"""

MUSIC_STATUS = """
on run argv
	set _d to (ASCII character 31)
	tell application id "com.apple.Music"
		set _state to (player state as text)
		if current track is missing value then
			return _state
		end if
		set _name to (name of current track) as text
		set _artist to (artist of current track) as text
		set _album to (album of current track) as text
		set _posText to ""
		try
			set _posText to ((player position) as integer as text)
		end try
		set _durText to ""
		try
			set _durText to ((duration of current track) as integer as text)
		end try
		return _state & _d & _name & _d & _artist & _d & _album & _d & _posText & _d & _durText
	end tell
end run
"""

MUSIC_PLAY = """
on run argv
	tell application id "com.apple.Music"
		play
	end tell
	return "ok"
end run
"""

MUSIC_PAUSE = """
on run argv
	tell application id "com.apple.Music"
		pause
	end tell
	return "ok"
end run
"""

MUSIC_NEXT = """
on run argv
	tell application id "com.apple.Music"
		next track
	end tell
	return "ok"
end run
"""

MUSIC_PREVIOUS = """
on run argv
	tell application id "com.apple.Music"
		previous track
	end tell
	return "ok"
end run
"""

MUSIC_PLAY_LIST = """
on run argv
	set _name to (item 1 of argv) as text
	tell application id "com.apple.Music"
		set _matches to (every playlist whose name is _name)
		if (count of _matches) is 0 then
			return "not_found"
		end if
		play (item 1 of _matches)
	end tell
	return "ok"
end run
"""

MUSIC_SEARCH_STORE = """
on run argv
	set _query to (item 1 of argv) as text
	set _maxCount to (item 2 of argv) as integer
	set _rows to {}
	tell application id "com.apple.Music"
		with timeout of 6 seconds
			try
				set _found to (search for _query)
				repeat with _tr in _found
					if (count of _rows) is greater than or equal to (_maxCount * 3) then exit repeat
					set end of _rows to (name of _tr)
					set end of _rows to (artist of _tr)
					set end of _rows to (album of _tr)
				end repeat
			end try
		end timeout
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_rows as text)
end run
"""

MUSIC_VOLUME_GET = """
on run argv
	tell application id "com.apple.Music"
		return ((sound volume) as text)
	end tell
end run
"""

MUSIC_VOLUME_SET = """
on run argv
	set _level to (item 1 of argv) as integer
	tell application id "com.apple.Music"
		set sound volume to _level
		return ((sound volume) as text)
	end tell
end run
"""

# Mail read-level-1 scripts. All outputs are STRING-ONLY (character id 31
# delimiters — same grammar as CALENDAR_LIST_EVENTS). Structured failures are
# RETURNED as stdout markers (see _raise_if_mail_marker): a named account that
# does not exist returns the NO_ACCOUNT_MARKER literal, a named mailbox that
# matches ZERO mailboxes across the scanned accounts returns the
# MAIL_MISSING_MAILBOX_MARKER literal, any other AppleScript error the
# MAIL_ERROR_MARKER literal (rc 0) — EXCEPT TCC denials (-1743), which are
# re-raised so they classify as consent problems. A named mailbox that EXISTS
# in some but not all accounts is NOT an error (mailboxFound short-circuits
# the marker: partial presence counts normally). Read-level-1 fields only:
# unread counts, mailbox names, subject/sender/received date. NO bodies, NO
# send/delete/save/move (intentionally unimplemented — see module docstring).
MAIL_UNREAD_COUNT = """
on run {mailboxArg, accountArg}
	set unreadTotal to 0
	set mailboxFound to false
	tell application id "com.apple.mail"
		try
			set targetAccounts to every account
			if accountArg is not "" then
				set namedAccounts to every account whose name is accountArg
				if (count of namedAccounts) is 0 then return "connectors-no-notes-account"
				set targetAccounts to namedAccounts
			end if
			repeat with acct in targetAccounts
				if mailboxArg is "" then
					set sourceMailboxes to every mailbox of acct whose name is "INBOX"
				else
					set sourceMailboxes to every mailbox of acct whose name is mailboxArg
					if (count of sourceMailboxes) is greater than 0 then set mailboxFound to true
				end if
				repeat with mbx in sourceMailboxes
					set unreadTotal to unreadTotal + (count of (messages of mbx whose read status is false))
				end repeat
			end repeat
			if mailboxArg is not "" and not mailboxFound then return "connectors-mail-missing-mailbox"
			return (unreadTotal as text)
		on error errorMessage number errorNumber
			if errorNumber is -1743 then error number -1743
			return "connectors-mail-error"
		end try
	end tell
end run
"""

MAIL_MAILBOXES = """
tell application id "com.apple.mail"
	try
		set mailboxNames to {}
		repeat with acct in every account
			repeat with mbx in every mailbox of acct
				set end of mailboxNames to name of mbx
			end repeat
		end repeat
		set AppleScript's text item delimiters to character id 31
		return mailboxNames as text
	on error errorMessage number errorNumber
		if errorNumber is -1743 then error number -1743
		return "connectors-mail-error"
	end try
end tell
"""

MAIL_LIST_RECENT = """
on run {mailboxArg, limitArg}
	set limitCount to limitArg as integer
	set mailboxFound to false
	set collectedMessages to {}
	tell application id "com.apple.mail"
		try
			repeat with acct in every account
				if mailboxArg is "" then
					set sourceMailboxes to every mailbox of acct whose name is "INBOX"
				else
					set sourceMailboxes to every mailbox of acct whose name is mailboxArg
					if (count of sourceMailboxes) is greater than 0 then set mailboxFound to true
				end if
				repeat with mbx in sourceMailboxes
					set availableMessages to messages of mbx
					if (count of availableMessages) > 0 then
						set takeCount to limitCount - (count of collectedMessages)
						if takeCount > (count of availableMessages) then set takeCount to (count of availableMessages)
						if takeCount > 0 then
							repeat with msg in items 1 through takeCount of availableMessages
								set end of collectedMessages to {(subject of msg) as text, (sender of msg) as text, (date received of msg) as text}
							end repeat
						end if
					end if
				end repeat
			end repeat
			if mailboxArg is not "" and not mailboxFound then return "connectors-mail-missing-mailbox"
			set rowItems to {}
			repeat with msgRef in collectedMessages
				set end of rowItems to item 1 of msgRef
				set end of rowItems to item 2 of msgRef
				set end of rowItems to item 3 of msgRef
			end repeat
			set AppleScript's text item delimiters to character id 31
			return rowItems as text
		on error errorMessage number errorNumber
			if errorNumber is -1743 then error number -1743
			return "connectors-mail-error"
		end try
	end tell
end run
"""

# ---------------------------------------------------------------------------
# Contacts + Messages (FASE 3) — frozen scripts, argv-only inputs.
# Real-Mac grammar, sandbox-unverifiable (same policy as the MAIL_* block).
# ---------------------------------------------------------------------------

#: Contacts.search: argv[0] = query, argv[1] = limit. Matches people by name
#: (case-insensitive `contains`), emits up to `limit` flat rows
#: name\org\primary-email\primary-phone with "" for missing fields.
CONTACTS_SEARCH = """
on run argv
	set _query to (item 1 of argv) as text
	set _lim to (item 2 of argv) as integer
	set _fields to {}
	tell application id "com.apple.AddressBook"
		repeat with _p in (every person whose name contains _query)
			if _lim is 0 then exit repeat
			set _org to ""
			if (organization of _p) is not missing value then set _org to (organization of _p) as text
			set _email to ""
			set _emails to (value of every email of _p)
			if (count of _emails) is greater than 0 then set _email to (item 1 of _emails) as text
			set _phone to ""
			set _phones to (value of every phone of _p)
			if (count of _phones) is greater than 0 then set _phone to (item 1 of _phones) as text
			set _fields to _fields & {((name of _p) as text), _org, _email, _phone}
			set _lim to _lim - 1
		end repeat
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_fields as text)
end run
"""

#: Contacts.get: argv[0] = EXACT contact name. Emits one 5-field record:
#: name\org\emails ", "-joined\phones ", "-joined\notes. No match -> the
#: rc-0 stdout marker (mirrored constant above).
CONTACTS_GET = """
on run argv
	set _name to (item 1 of argv) as text
	tell application id "com.apple.AddressBook"
		set _people to (every person whose name is _name)
		if (count of _people) is 0 then return "connectors-contacts-not-found"
		set _p to (item 1 of _people)
		set _org to ""
		if (organization of _p) is not missing value then set _org to (organization of _p) as text
		set _emails to {}
		set _elist to (value of every email of _p)
		if (count of _elist) is greater than 0 then
			repeat with _e in _elist
				set _emails to _emails & {(_e as text)}
			end repeat
		end if
		set _phones to {}
		set _plist to (value of every phone of _p)
		if (count of _plist) is greater than 0 then
			repeat with _ph in _plist
				set _phones to _phones & {(_ph as text)}
			end repeat
		end if
		set _note to ""
		if (note of _p) is not missing value then set _note to (note of _p) as text
	end tell
	set AppleScript's text item delimiters to {", "}
	set _emails_text to (_emails as text)
	set _phones_text to (_phones as text)
	set AppleScript's text item delimiters to ASCII character 31
	return ({(name of _p as text), _org, _emails_text, _phones_text, _note} as text)
end run
"""

#: Contacts.create: argv [name, org, e1, e2, e3, p1, p2, p3] (absent items ride
#: as ""). Creates the person; non-empty emails/phones are added in order.
#: (delete/update are intentionally NOT implemented — destrutivos.)
CONTACTS_CREATE = """
on run argv
	set _name to (item 1 of argv) as text
	set _org to (item 2 of argv) as text
	tell application id "com.apple.AddressBook"
		set _p to make new person with properties {first name:_name}
		if _org is not "" then set organization of _p to _org
		repeat with _e in {(item 3 of argv) as text, (item 4 of argv) as text, (item 5 of argv) as text}
			if _e is not "" then
				make new email at end of emails of _p with properties {label:"outro", value:_e}
			end if
		end repeat
		repeat with _ph in {(item 6 of argv) as text, (item 7 of argv) as text, (item 8 of argv) as text}
			if _ph is not "" then
				make new phone at end of phones of _p with properties {label:"outro", value:_ph}
			end if
		end repeat
	end tell
	return "created"
end run
"""

#: Messages.send_message (raw-handle variant): argv [to, text] — `to` is a
#: pre-validated raw handle (e-mail or +E.164), sent straight through.
MESSAGES_SEND_HANDLE = """
on run argv
	set _to to (item 1 of argv) as text
	set _msg to (item 2 of argv) as text
	tell application id "com.apple.iChat"
		send _msg to participant _to
	end tell
	return "sent"
end run
"""

#: Messages.send_message (contact-lookup variant): argv [to, text] — `to` is a
#: contact NAME; the script resolves it via Contacts (a SINGLE exact-name
#: match -> first email, else first phone) and refuses (rc-0 marker) when the
#: name is ambiguous (multiple people share it — never guess a recipient) or
#: the match produces nothing sendable. SEND GUARD lives here; the Python side
#: validates shapes and maps the marker to the refusal copy.
MESSAGES_SEND_CONTACT = """
on run argv
	set _to to (item 1 of argv) as text
	set _msg to (item 2 of argv) as text
	set _handle to ""
	tell application id "com.apple.AddressBook"
		set _matches to (every person whose name is _to)
		-- Ambiguity refusal: more than one exact-name match -> refuse (never
		-- guess a recipient); exactly one match may resolve a handle.
		if (count of _matches) is greater than 1 then return "connectors-messages-unknown-recipient"
		if (count of _matches) is 1 then
			set _p to (item 1 of _matches)
			set _elist to (value of every email of _p)
			if (count of _elist) is greater than 0 then set _handle to (item 1 of _elist) as text
			if _handle is "" then
				set _plist to (value of every phone of _p)
				if (count of _plist) is greater than 0 then set _handle to (item 1 of _plist) as text
			end if
		end if
	end tell
	if _handle is "" then return "connectors-messages-unknown-recipient"
	tell application id "com.apple.iChat"
		send _msg to participant _handle
	end tell
	return "sent"
end run
"""

#: Messages.list_recent_threads: argv[0] = count (1..10). Emits up to `count`
#: flat rows handle\last-message-DATE-ONLY (YYYY-MM-DD via integer date
#: components — locale-proof, mirrors the REMINDERS_ADD integer-argv trick in
#: reverse). Message TEXT is never read — date-only privacy floor.
MESSAGES_LIST_THREADS = """
on run argv
	set _lim to (item 1 of argv) as integer
	set _fields to {}
	tell application id "com.apple.iChat"
		repeat with _c in (every chat)
			if _lim is 0 then exit repeat
			set _d to (date of (last message of _c))
			set _m to text -2 thru -1 of ("0" & ((month of _d) as integer as text))
			set _day to text -2 thru -1 of ("0" & ((day of _d) as text))
			set _fields to _fields & {((id of _c) as text), (((year of _d) as text) & "-" & _m & "-" & _day)}
			set _lim to _lim - 1
		end repeat
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_fields as text)
end run
"""

# ---------------------------------------------------------------------------
# Finder + QuickTime (FASE 4) — frozen scripts, argv-only inputs.
# Real-Mac grammar, sandbox-unverifiable (same policy as the MAIL_* block);
# the QuickTime grammar is sdef-verified on this host (no with-audio param on
# "new screen recording" — audio rides the document's `current microphone`).
# ---------------------------------------------------------------------------

#: Finder.list: argv[0] = folder path (already root-validated by the builder).
#: Emits item NAMES joined with ASCII character 31 (flat list, no empty fields):
#: the Python post trims to FINDER_LIST_MAX_ITEMS. A missing or non-folder path
#: (folder coercion of a file errors) returns the not-found marker.
FINDER_LIST = """
on run argv
	set _root to (item 1 of argv) as text
	tell application id "com.apple.finder"
		try
			set _folder to folder (POSIX file _root as alias)
		on error
			return "connectors-finder-not-found"
		end try
		set _names to (name of items of _folder)
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_names as text)
end run
"""

#: Finder.info: argv[0] = item path. Emits ONE row of FIVE fields
#: kind \x1f size \x1f extension \x1f created \x1f modified ("" for empty size
#: of a folder / missing name extension). Missing path -> not-found marker.
FINDER_INFO = """
on run argv
	set _path to (item 1 of argv) as text
	tell application id "com.apple.finder"
		try
			set _item to (POSIX file _path as alias)
		on error
			return "connectors-finder-not-found"
		end try
		set _kind to (kind of _item) as text
		if (size of _item) is missing value then
			set _size to ""
		else
			set _size to (size of _item) as text
		end if
		if (name extension of _item) is missing value then
			set _ext to ""
		else
			set _ext to (name extension of _item) as text
		end if
		set _created to (creation date of _item) as text
		set _modified to (modification date of _item) as text
	end tell
	set AppleScript's text item delimiters to ASCII character 31
	return (_kind & ASCII character 31 & _size & ASCII character 31 & _ext & ASCII character 31 & _created & ASCII character 31 & _modified)
end run
"""

#: Finder.move: argv[0] = src path, argv[1] = destination DIRECTORY, argv[2] =
#: basename(src) (computed Python-side — no shell anywhere). Refuses WITHOUT
#: moving: destination dir missing or not a directory -> dst-dir-missing marker;
#: an item of the same name already in dst -> dst-exists marker (never
#: overwrite); missing source -> not-found marker. `move` = Finder's move
#: (never a copy+delete of a foreign volume).
FINDER_MOVE = """
on run argv
	set _src to (item 1 of argv) as text
	set _dstDir to (item 2 of argv) as text
	set _name to (item 3 of argv) as text
	tell application id "com.apple.finder"
		try
			set _dstFolder to folder (POSIX file _dstDir as alias)
		on error
			return "connectors-finder-dst-dir-missing"
		end try
		if (exists item _name of _dstFolder) then
			return "connectors-finder-dst-exists"
		end if
		try
			set _srcItem to (POSIX file _src as alias)
		on error
			return "connectors-finder-not-found"
		end try
		move _srcItem to _dstFolder
	end tell
	return "moved"
end run
"""

#: Finder.trash: argv[0] = item path (Python side already refuses the home
#: root and outside-roots rules). Finder's `delete` MOVES TO TRASH — never a
#: void delete; the destructive double-arm dialog is UI-level only.
FINDER_TRASH = """
on run argv
	set _path to (item 1 of argv) as text
	tell application id "com.apple.finder"
		try
			set _item to (POSIX file _path as alias)
		on error
			return "connectors-finder-not-found"
		end try
		delete _item
	end tell
	return "trashed"
end run
"""

#: QuickTime.start_recording: argv[0] = "1" when with_audio. `new screen
#: recording` returns NO document (sdef) -> grab `document 1` (the just-created
#: front doc); with_audio sets the doc's current microphone inside a benign
#: try (device list empty -> recording still starts, just silent).
#: Returns the doc id — the Python post REMEMBERS it (process memory).
QUICKTIME_NEW_AUDIO = """
on run argv
	tell application id "com.apple.QuickTimePlayerX"
		activate
		set _doc to (new audio recording)
		start _doc
		return (id of _doc)
	end tell
end run
"""

QUICKTIME_STOP = """
on run argv
	set _docId to (item 1 of argv) as text
	tell application id "com.apple.QuickTimePlayerX"
		try
			set _doc to document id _docId
		on error
			return "connectors-qt-doc-gone"
		end try
		stop _doc
		if (file of _doc) is missing value then
			return "connectors-qt-doc-gone"
		end if
		return (POSIX path of (file of _doc))
	end tell
end run
"""

QUICKTIME_START_SCREEN = """
on run argv
	set _audio to ((item 1 of argv) is "1")
	tell application id "com.apple.QuickTimePlayerX"
		activate
		new screen recording
		-- new screen recording opens a setup window; wait for the actual document (30s)
		set _doc to missing value
		repeat with i from 1 to 30
			delay 1
			try
				set _doc to document 1
				exit repeat
			on error
			end try
		end repeat
		if _doc is missing value then
			return "connectors-qt-doc-timeout"
		end if
		if _audio then
			try
				set current microphone of _doc to audio recording device 1
			end try
		end if
		start _doc
		return (id of _doc)
	end tell
end run
"""

#: QuickTime.play: argv[0] = file path (Python side requires an existing file).
#: `open` returns the document; `play` starts it.
QUICKTIME_PLAY = """
on run argv
	set _path to (item 1 of argv) as text
	tell application id "com.apple.QuickTimePlayerX"
		activate
		set _doc to (open (POSIX file _path))
		play _doc
	end tell
end run
"""

#: Static script source per (domain, action) — the model can never influence
#: these bytes; its strings ride strictly in argv. (system.volume splits into
#: get/set script variants at argv-build time.)
SCRIPTS: Dict[Tuple[str, str], str] = {
    ("reminders", "list_lists"): REMINDERS_LIST_LISTS,
    ("reminders", "list_reminders"): REMINDERS_LIST_REMINDERS,
    ("reminders", "add"): REMINDERS_ADD,
    ("reminders", "complete"): REMINDERS_COMPLETE,
    ("notes", "search"): NOTES_SEARCH,
    ("notes", "read"): NOTES_READ,
    ("notes", "create"): NOTES_CREATE,
    ("calendar", "list_calendars"): CALENDAR_LIST_CALENDARS,
    ("calendar", "list_events"): CALENDAR_LIST_EVENTS,
    ("calendar", "add_event"): CALENDAR_ADD_EVENT,
    ("calendar", "delete_event"): CALENDAR_DELETE_EVENT,
    ("music", "status"): MUSIC_STATUS,
    ("music", "play"): MUSIC_PLAY,
    ("music", "pause"): MUSIC_PAUSE,
    ("music", "next"): MUSIC_NEXT,
    ("music", "previous"): MUSIC_PREVIOUS,
    ("music", "play_playlist"): MUSIC_PLAY_LIST,
    ("music", "search_store"): MUSIC_SEARCH_STORE,
    ("music", "volume"): MUSIC_VOLUME_GET,
    ("music", "volume_set"): MUSIC_VOLUME_SET,
    ("mail", "unread_count"): MAIL_UNREAD_COUNT,
    ("mail", "mailboxes"): MAIL_MAILBOXES,
    ("mail", "list_recent"): MAIL_LIST_RECENT,
    ("contacts", "search"): CONTACTS_SEARCH,
    ("contacts", "get"): CONTACTS_GET,
    ("contacts", "create"): CONTACTS_CREATE,
    # send_message splits handle/contact variants at argv-build time (the
    # system.volume get/set precedent); the _contact key is non-whitelisted.
    ("messages", "send_message"): MESSAGES_SEND_HANDLE,
    ("messages", "send_message_contact"): MESSAGES_SEND_CONTACT,
    ("messages", "list_recent_threads"): MESSAGES_LIST_THREADS,
    ("finder", "list"): FINDER_LIST,
    ("finder", "info"): FINDER_INFO,
    ("finder", "move"): FINDER_MOVE,
    ("finder", "trash"): FINDER_TRASH,
    ("quicktime", "start_recording"): QUICKTIME_START_SCREEN,
    ("quicktime", "new_audio_recording"): QUICKTIME_NEW_AUDIO,
    ("quicktime", "stop_recording"): QUICKTIME_STOP,
    ("quicktime", "play"): QUICKTIME_PLAY,
    ("system", "volume"): SYSTEM_VOLUME_GET,
    ("system", "volume_set"): SYSTEM_VOLUME_SET,
    ("system", "front_app"): SYSTEM_FRONT_APP,
    ("system", "toast"): SYSTEM_TOAST,
}

# Minimal probe scripts: one trivial consented touch per domain (<=8 s timeout).
PROBE_SCRIPTS: Dict[str, str] = {
    "reminders": """
on run argv
	tell application id "com.apple.reminders"
		return ((count lists) as text)
	end tell
end run
""",
    "notes": """
on run argv
	tell application id "com.apple.Notes"
		return ((count accounts) as text)
	end tell
end run
""",
    "calendar": """
on run argv
	tell application id "com.apple.iCal"
		return (count calendars as text)
	end tell
end run
""",
    # Music's probe is try-wrapped only to survive "no current track": TCC
    # denials (-1743) are re-raised so probe() classifies them as `denied`
    # instead of a ghost `granted`. Consent is still triggered by the `tell`.
    "music": """
on run argv
	tell application id "com.apple.Music"
		try
			return ((name of current track is missing value) as text)
		on error errMsg number errNum
			-- re-raise TCC denials: probe() maps a -1743 exit to "denied";
			-- swallow ONLY benign misses (e.g. no current track, -1728).
			if errNum is -1743 then error errMsg number errNum
			return "false"
		end try
	end tell
end run
""",
    # Mail: trivial consented touch — account/mailbox counting proves the
    # automation consent; output content is not consumed by classification.
    # (Real-Mac grammar, sandbox-unverifiable: "count unregistered accounts".)
    "mail": """
tell application id "com.apple.mail"
	try
		return ((count unregistered accounts) as text) & "," & ((count of mailboxes of account 1) as text)
	on error errorMessage number errorNumber
		if errorNumber is -1743 then error number -1743
		return "0,0"
	end try
end tell
""",
    # Contacts/Messages: trivial consented touches (Automation -> Contacts /
    # Automation -> Messages); the tells fire macOS TCC consent.
    "contacts": """
on run argv
	tell application id "com.apple.AddressBook"
		return (count of people as text)
	end tell
end run
""",
    "messages": """
on run argv
	tell application id "com.apple.iChat"
		return (count of services as text)
	end tell
end run
""",
    # Finder: trivial consented touch (Automation -> Finder); fires TCC consent.
    "finder": """
on run argv
	tell application id "com.apple.finder"
		return (count of desktop items as text)
	end tell
end run
""",
    # QuickTime: trivial consented touch (Automation -> QuickTime); the FIRST
    # screen recording additionally fires Screen Recording TCC, which the
    # ACTION classifies (the probe stays automation-level, like music's).
    "quicktime": """
on run argv
	tell application id "com.apple.QuickTimePlayerX"
		return (count windows as text)
	end tell
end run
""",
    # system needs no consent; the volume read is a harmless osascript sanity probe.
    "system": SYSTEM_VOLUME_GET,
}

# CLI-based probes (no osascript): prove CLI availability by exit code.
# Consumed by probe() BEFORE the hardcoded ["osascript", "-e", script] path.
PROBE_COMMANDS: Dict[str, List[str]] = {"shortcuts": ["shortcuts", "list"]}

# Bundle ids keep the scripts immune to pt-BR app-name localization. (system's
# front_app touches System Events; volume uses scripting additions only;
# shortcuts is a CLI domain with no app id.)
BUNDLE_IDS: Dict[str, str] = {
    "reminders": "com.apple.reminders",
    "notes": "com.apple.Notes",
    "calendar": "com.apple.iCal",
    "music": "com.apple.Music",
    "mail": "com.apple.mail",
    "contacts": "com.apple.AddressBook",
    "messages": "com.apple.iChat",
    "finder": "com.apple.finder",
    "quicktime": "com.apple.QuickTimePlayerX",
    "system": "com.apple.systemevents",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hint_for(status: str) -> str:
    if status == STATUS_GRANTED:
        return HINT_GRANTED
    if status == STATUS_NEEDS_CONSENT:
        return HINT_NEEDS_CONSENT
    if status == STATUS_DENIED:
        return HINT_DENIED
    return HINT_UNKNOWN


class _Outcome(NamedTuple):
    """Runner seam result shape (CompletedProcess-compatible subset)."""

    returncode: int
    stdout: str
    stderr: str


def _default_runner(cmd: List[str], timeout: float, stdin: Optional[str] = None) -> Any:
    """Production bridge (osascript/open/shortcuts CLI): list-form argv (no
    shell, ever). stdin text rides subprocess input= (shortcuts -x)."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)


def _is_denial_stderr(stderr: str) -> bool:
    """osascript TCC-denial markers (exit -1743 family)."""
    lowered = (stderr or "").lower()
    return "-1743" in lowered or "not authorized" in lowered


def _split_delimited(stdout: str) -> List[str]:
    """Split a delimiter-joined script result; empty strings dropped."""
    return [item for item in (stdout or "").split(_DELIM) if item != ""]


def _split_lines(stdout: str) -> List[str]:
    """Split CLI stdout into non-empty stripped lines (shortcuts list)."""
    return [line.strip() for line in (stdout or "").split("\n") if line.strip()]


def _raise_if_mail_marker(stdout: str) -> None:
    """Map the frozen mail scripts' stdout markers (rc 0 — the scripts RETURN
    them instead of raising, mirroring the music "not_found" stdout
    convention) to clean user-facing errors. Equality-after-strip: an email
    subject that merely CONTAINS a marker token can never false-positive."""
    text = (stdout or "").strip()
    if text == MAIL_MISSING_MAILBOX_MARKER:
        raise ConnectorError(
            'Mailbox não encontrada — use a ação "mailboxes" para ver os nomes '
            "disponíveis."
        )
    if text == MAIL_ERROR_MARKER:
        raise ConnectorError("Mail action failed: unexpected AppleScript error.")
    if text == NO_ACCOUNT_MARKER:
        raise ConnectorError(
            "Nenhuma conta configurada — configure uma conta no Mail primeiro."
        )


def _truncate_bytes(text: str, cap: int) -> str:
    """Byte-cap a string on a UTF-8 boundary (never splits a code point)."""
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    return encoded[:cap].decode("utf-8", errors="ignore")


def _as_runner_text(value: Any) -> str:
    """Normalize a runner's stdout/stderr (CompletedProcess exposes str under
    text=True; defensive str() keeps custom seams honest)."""
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _parse_level_stdout(stdout: str) -> int:
    try:
        return int((stdout or "").strip())
    except ValueError:
        raise ConnectorError("volume result was not an integer (osascript output changed)") from None


def _parse_count_stdout(stdout: str) -> int:
    try:
        return int((stdout or "").strip())
    except ValueError:
        raise ConnectorError("delete count was not an integer (osascript output changed)") from None


def _parse_unread_stdout(stdout: str) -> int:
    """Mail unread_count stdout: marker guard + integer (read-level-1 count)."""
    text = (stdout or "").strip()
    _raise_if_mail_marker(text)
    try:
        return int(text)
    except ValueError:
        raise ConnectorError("unread count was not an integer (osascript output changed)") from None


def _parse_status_stdout(stdout: str) -> dict:
    """state\x1fname\x1fartist\x1falbum\x1fposition\x1fduration -> normalized dict.
    RAW .split on the delimiter (NOT _split_delimited: empty track fields must
    keep their positions). Missing track -> state only, defaults elsewhere."""
    parts = (stdout or "").strip().split(_DELIM)

    def field(i: int) -> str:
        return parts[i] if len(parts) > i else ""

    def int_field(i: int) -> int:
        try:
            return int(field(i))
        except ValueError:
            return 0

    state = parts[0] if parts and parts[0] else "stopped"
    return {
        "state": state,
        "name": field(1),
        "artist": field(2),
        "album": field(3),
        "position": int_field(4),
        "duration": int_field(5),
    }


class ConnectorError(Exception):
    """User-visible connector failure. Message is PT-BR where user-facing
    (consent/denial/timeout/not-found); English for validation/infrastructure."""


class _ScriptFailure(Exception):
    """Internal (pre-message) failure classification from the runner seam."""

    def __init__(self, kind: str, returncode: int, stderr: str):
        super().__init__(kind)
        self.kind = kind  # "timeout" | "denied" | "error"
        self.returncode = int(returncode)
        self.stderr = stderr


class ProbeBusy(Exception):
    """Raised when a probe is already running for the same domain (API: 409)."""


class ConnectorsStateStore:
    """Per-domain consent state under ``<config_dir>/data/connectors_state.json``
    (sibling of ``data/agent_sessions/``). Runtime-only file — never stores
    secrets (v1 domains hold none), never enters git (gitignored)."""

    def __init__(self, config_dir: str):
        self.config_dir = config_dir
        self.path = os.path.join(config_dir, "data", "connectors_state.json")
        self._lock = threading.Lock()

    def read(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Fresh read: per-domain ``{status, last_probe}``; absent/bad values
        degrade to ``unknown``/None. Atomic writes keep readers torn-file-free."""
        with self._lock:
            raw = self._read_unlocked()
        rows: Dict[str, Dict[str, Optional[str]]] = {}
        for domain in CONNECTOR_DOMAINS:
            entry = raw.get(domain) if isinstance(raw.get(domain), dict) else {}
            status = entry.get("status")
            last_probe = entry.get("last_probe")
            rows[domain] = {
                "status": status if status in VALID_STATUSES else STATUS_UNKNOWN,
                "last_probe": last_probe if isinstance(last_probe, str) else None,
            }
        return rows

    def record(self, domain: str, status: str) -> Dict[str, Any]:
        """Persist one domain under the write lock (read-modify-write +
        ``_write_atomic``); returns the stored ``{status, last_probe}`` row."""
        if domain not in CONNECTOR_DOMAINS:
            raise ValueError(f"unknown connector domain: {domain!r}")
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid connector status: {status!r}")
        stamp = _utcnow_iso()
        with self._lock:
            data = self._read_unlocked()
            data[domain] = {"status": status, "last_probe": stamp}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            _write_atomic(self.path, json.dumps(data, indent=2, ensure_ascii=False))
        return {"status": status, "last_probe": stamp}

    def _read_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as read_file:
                data = json.load(read_file)
        except (OSError, ValueError):
            return {}  # a corrupted store rebuilds on the next record()
        return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Argument validation helpers (all errors English, ToolFailure-style)
# ---------------------------------------------------------------------------


def _is_email_handle(text: str) -> bool:
    """Raw-handle check for messages.send_message: a loose e-mail shape;
    control characters (the C0 range plus DEL) are excluded so a NUL argv
    item fails validation here instead of surfacing as an ugly exec-time
    ValueError."""
    return re.fullmatch(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+$", text) is not None


def _is_e164_handle(text: str) -> bool:
    """Raw-handle check for messages.send_message: strict ITU E.164
    (+ country code 1-9, 1..14 total digits, nothing else)."""
    return re.fullmatch(r"^\+[1-9]\d{1,14}$", text) is not None


def _optional_capped_int(value: Any, field: str, cap: int, default: int, units: str) -> int:
    """Optional positive int with a hard cap: absent/None -> default; above the
    cap ERRORS (the _as_days / mail count precedent — never clamped down)."""
    if isinstance(value, (dict, list)):
        raise ConnectorError(f"'{field}' must be a positive integer")
    number = _as_positive_int(value, field)
    if number is None:
        return default
    if number > cap:
        raise ConnectorError(f"{field} accepts at most {cap} {units}.")
    return number


def _finder_allowed_roots() -> List[str]:
    """Canonical allowed roots for finder path inputs (expanduser+realpath, no
    existence requirement — the prefix compare is string-based)."""
    return [os.path.realpath(os.path.expanduser(root)) for root in _FINDER_ALLOWED_ROOTS]


def _safe_expand_resolved(raw_path: str, field: str) -> str:
    """expanduser + realpath with the NUL/relative guards the handle validators
    use (_is_email_handle control-char precedent): os.path raises uncaught
    ValueError on embedded NULs, and CWD-relative paths would make the allowed-
    roots compare process-state dependent — both refuse as clean ConnectorError
    validation copies instead."""
    if raw_path.strip() == "" or "\x00" in raw_path:
        raise ConnectorError(f"'{field}' must be a non-empty path")
    expanded = os.path.expanduser(raw_path)
    if not os.path.isabs(expanded):
        raise ConnectorError(
            f"'{field}' must be an absolute path or begin with '~' — got {raw_path!r}"
        )
    return os.path.realpath(expanded)


def _resolve_under_allowed_root(raw_path: str, field: str) -> str:
    """finder list/info/reveal/move paths: expanduser -> realtime-resolve
    (kills '~/..' and symlink tricks) -> require one allowed-root prefix."""
    resolved = _safe_expand_resolved(raw_path, field)
    for root in _finder_allowed_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    raise ConnectorError(ERR_PATH_OUTSIDE_ROOTS.format(path=resolved))


def _resolve_trash_path(raw_path: str) -> str:
    """finder.trash rule (mission): refuse the home root; refuse anything
    outside the allowed roots UNLESS it exists and is a plain file."""
    resolved = _safe_expand_resolved(raw_path, "path")
    home = os.path.realpath(os.path.expanduser("~"))
    if resolved == home:
        raise ConnectorError(ERR_TRASH_HOME_REFUSED.format(path=resolved))
    for root in _finder_allowed_roots():
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    if os.path.isfile(resolved):
        return resolved
    raise ConnectorError(ERR_PATH_OUTSIDE_ROOTS.format(path=resolved))


def _as_string(value: Any, field: str, field_max: Optional[int] = None) -> str:
    """Coerce a model-supplied JSON value to a validated, non-empty string."""
    if value is None or isinstance(value, (bool, dict, list)):
        raise ConnectorError(f"'{field}' must be a non-empty string")
    if isinstance(value, (int, float)):
        value = str(value)
    if not isinstance(value, str):
        raise ConnectorError(f"'{field}' must be a non-empty string")
    text = value.strip()
    if text == "":
        raise ConnectorError(f"'{field}' must be a non-empty string")
    if field_max is not None and len(text) > field_max:
        raise ConnectorError(f"'{field}' is limited to {field_max} characters")
    return text


def _as_limit(value: Any) -> int:
    """limit: default 20, cap 50 (values above the cap clamp down), 1..50."""
    if value is None:
        return LIST_LIMIT_DEFAULT
    if isinstance(value, bool):
        raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
        value = int(value)
    if isinstance(value, int):
        limit = value
    elif isinstance(value, str) and value.strip().isdigit():
        limit = int(value.strip())
    else:
        raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
    if limit < 1:
        raise ConnectorError(f"'limit' must be an integer between 1 and {LIST_LIMIT_MAX}")
    return min(limit, LIST_LIMIT_MAX)


def _parse_date(value: Any, field: str) -> Optional[Tuple[int, int, int]]:
    """``YYYY-MM-DD`` -> (year, month, day); None/'' -> None (no date)."""
    if value is None:
        return None
    if isinstance(value, (bool, dict, list)):
        raise ConnectorError(f"'{field}' must be a string formatted YYYY-MM-DD")
    text = str(value).strip()
    if text == "":
        return None
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        raise ConnectorError(f"'{field}' must be a string formatted YYYY-MM-DD")
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31):
        raise ConnectorError(f"'{field}' month must be 01-12 and day 01-31")
    return year, month, day


def _parse_due(value: Any) -> Optional[Tuple[int, int, int]]:
    """``YYYY-MM-DD`` -> (year, month, day); None/'' -> None (no due date)."""
    return _parse_date(value, "due")


def _parse_datetime_arg(value: Any, field: str) -> Tuple[int, int, int, int, int]:
    """``YYYY-MM-DD HH:MM`` -> (year, month, day, hour, minute)."""
    if value is None or isinstance(value, (bool, dict, list)):
        raise ConnectorError(f"'{field}' must be a string formatted YYYY-MM-DD HH:MM")
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})", text)
    if not match:
        raise ConnectorError(f"'{field}' must be a string formatted YYYY-MM-DD HH:MM")
    year, month, day, hour, minute = (int(g) for g in match.groups())
    if not (1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31):
        raise ConnectorError(f"'{field}' month must be 01-12 and day 01-31")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConnectorError(f"'{field}' hour must be 00-23 and minute 00-59")
    return year, month, day, hour, minute


def _as_days(value: Any) -> int:
    """days: default 7, VALID 1..30 (error, NOT clamp: >30 is a validation failure)."""
    if value is None:
        return DATERANGE_DEFAULT_DAYS
    if isinstance(value, bool):
        raise ConnectorError(f"'days' must be an integer between 1 and {DATERANGE_MAX_DAYS}")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConnectorError(f"'days' must be an integer between 1 and {DATERANGE_MAX_DAYS}")
        value = int(value)
    if isinstance(value, int):
        days = value
    elif isinstance(value, str) and value.strip().isdigit():
        days = int(value.strip())
    else:
        raise ConnectorError(f"'days' must be an integer between 1 and {DATERANGE_MAX_DAYS}")
    if not (1 <= days <= DATERANGE_MAX_DAYS):
        raise ConnectorError(f"'days' must be an integer between 1 and {DATERANGE_MAX_DAYS}")
    return days


def _as_positive_int(value: Any, field: str) -> Optional[int]:
    """alert_minutes: None -> None; otherwise a positive integer (bool rejected)."""
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return None
    if isinstance(value, bool):
        raise ConnectorError(f"'{field}' must be a positive integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConnectorError(f"'{field}' must be a positive integer")
        value = int(value)
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ConnectorError(f"'{field}' must be a positive integer")
    if number < 1:
        raise ConnectorError(f"'{field}' must be a positive integer")
    return number


def _chunk_rows(items: List[str], fields: Tuple[str, ...]) -> List[Dict[str, str]]:
    """Chunk a flat delimiter-split list into dicts; incomplete trailing rows dropped."""
    step = len(fields)
    rows: List[Dict[str, str]] = []
    for i in range(0, len(items) - step + 1, step):
        rows.append(dict(zip(fields, items[i:i + step])))
    return rows


def _as_level(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        raise ConnectorError("'level' must be an integer between 0 and 100")
    if isinstance(value, float):
        if not value.is_integer():
            raise ConnectorError("'level' must be an integer between 0 and 100")
        value = int(value)
    if isinstance(value, int):
        level = value
    elif isinstance(value, str) and value.strip().isdigit():
        level = int(value.strip())
    else:
        raise ConnectorError("'level' must be an integer between 0 and 100")
    if not (0 <= level <= 100):
        raise ConnectorError("'level' must be an integer between 0 and 100")
    return level


def _required_params(params: Dict[str, Any], fields: Tuple[str, ...]) -> None:
    for name in fields:
        if name not in params or params.get(name) is None:
            raise ConnectorError(f"'{name}' is required")


def _at_most_one(params: Dict[str, Any], fields: Tuple[str, ...], error: str) -> str:
    present = [name for name in fields if params.get(name) not in (None, "")]
    if len(present) != 1:
        raise ConnectorError(error)
    return present[0]


def _body_bytes_cap(value: Any, field: str) -> str:
    """Cap a body at BODY_MAX_BYTES (server-side; context cap applies on top)."""
    text = _as_string(value, field)
    return _truncate_bytes(text, BODY_MAX_BYTES)


class _ArgvPlan(NamedTuple):
    """A fully validated native.call invocation: the exact command list plus
    how to normalize the runner's stdout into the tool result. The trailing
    ``timeout``/``stdin`` fields default safely (existing constructions never
    pass positional extras); CLI domains (shortcuts) override the timeout and
    pipe validated text through the runner's stdin channel."""

    cmd: List[str]
    kind: str = "ok"  # ok | split | split_keep | send | complete | read | raw | level_get | level_set | playlist | count | status | unread | shortcuts_list | shortcuts_run | finder
    post: Optional[Callable[[dict], dict]] = None
    complete_list: str = ""
    complete_query: str = ""
    read_name: str = ""
    playlist_name: str = ""
    timeout: float = CONNECTORS_TIMEOUT_S
    stdin: Optional[str] = None


# ---------------------------------------------------------------------------
# ConnectorManager
# ---------------------------------------------------------------------------


class ConnectorManager:
    """Whitelist + consent gate + argv-bound osascript executor for native.call.

    One per server process (BotApiRuntime lazy singleton; ToolRegistry owns a
    reference so ``native.call`` never rebuilds it).
    """

    _Runner = Callable[[List[str], float, Optional[str]], Any]

    def __init__(self, config_dir: str, runner: Optional[_Runner] = None):
        self.config_dir = config_dir
        self.state = ConnectorsStateStore(config_dir)
        # Runner seam: explicit injection wins (tests); else the in-process
        # mock world when NOTIFYBOT_CONNECTORS_MOCK=1; else osascript.
        self._mock_mode = runner is None and os.environ.get(CONNECTORS_MOCK_ENV, "") == "1"
        if runner is not None:
            self._runner: Callable[[List[str], float, Optional[str]], Any] = runner
        elif self._mock_mode:
            self._runner = self._mock_runner
        else:
            self._runner = _default_runner
        self._probe_locks: Dict[str, threading.Lock] = {d: threading.Lock() for d in CONNECTOR_DOMAINS}
        # QuickTime recording slot: process memory ONLY (lost on daemon
        # restart — safe by design); one recording at a time.
        self._qt_recording: Optional[Dict[str, str]] = None
        # In-memory mock world (per manager == per server process; tests get a
        # fresh world per manager instance).
        self._mock_lock = threading.Lock()
        self._mock_lists: Dict[str, List[Dict[str, Any]]] = {}
        self._mock_notes: List[Dict[str, str]] = []
        self._mock_volume = 50
        self._mock_front_app = "Finder"
        self._mock_last_toast: Optional[Tuple[str, str]] = None
        self._mock_calendars: List[str] = ["Pessoal", "Trabalho"]
        self._mock_events: List[Dict[str, str]] = []
        self._mock_music: Dict[str, Any] = {
            "state": "paused", "name": "", "artist": "", "album": "",
            "position": 0, "duration": 0, "volume": 50,
            "playlists": ["Foco", "Treino"],
        }
        self._mock_shortcuts = {
            "names": ["Resumo da semana", "Abrir agenda", "Backup das fotos"],
        }
        self._mock_mail = {
            "unread": 3,
            "accounts": ["iCloud", "Gmail"],
            "mailboxes": ["INBOX", "Enviados", "Arquivo"],
            "recent": [
                ("Relatório mensal pronto", "Fulano <fulano@exemplo.com>", "31/10/2026 18:02"),
                ("Re: contratos", "Beltrano <beltrano@exemplo.com>", "01/11/2026 09:15"),
            ],
        }
        self._mock_contacts = {
            "people": [
                {"name": "Marina Duarte", "org": "Imaginária Ltda",
                 "emails": ["marina@imaginaria.test"],
                 "phones": ["+5511922220001"], "note": "Prefere contato por e-mail."},
                {"name": "Bruno Tanaka", "org": "",
                 "emails": ["bruno@tanaka.test"], "phones": [], "note": ""},
            ],
        }
        self._mock_threads = [
            ("marina@imaginaria.test", "2026-11-07"),
            ("+5511922220001", "2026-11-06"),
        ]
        _desktop = os.path.realpath(os.path.expanduser("~/Desktop"))
        _downloads = os.path.realpath(os.path.expanduser("~/Downloads"))
        self._mock_finder = {
            "folders": {
                _desktop: ["Relatório.pdf", "Captura de Tela.png", "trabalho"],
                _downloads: ["Relatório.pdf"],
            },
            "items": {
                os.path.join(_desktop, "Relatório.pdf"): {
                    "kind": "PDF document", "size": "20480", "extension": "pdf",
                    "created": "31/10/2026 18:02", "modified": "01/11/2026 09:15",
                },
                os.path.join(_desktop, "Captura de Tela.png"): {
                    "kind": "PNG image", "size": "512000", "extension": "png",
                    "created": "01/11/2026 08:00", "modified": "01/11/2026 08:00",
                },
                os.path.join(_desktop, "trabalho"): {
                    "kind": "Folder", "size": "", "extension": "",
                    "created": "30/10/2026 12:00", "modified": "02/11/2026 10:30",
                },
                os.path.join(_downloads, "Relatório.pdf"): {
                    "kind": "PDF document", "size": "1024", "extension": "pdf",
                    "created": "02/11/2026 10:00", "modified": "02/11/2026 10:00",
                },
            },
        }
        self._mock_qt_seq = 0

    # ---- public API -------------------------------------------------------

    def rows(self) -> dict:
        """GET /api/connectors payload: per-domain status/granted/last_probe/hint.
        ``system`` is always granted (no consent required)."""
        store_rows = self.state.read()
        connectors: Dict[str, Any] = {}
        for domain in CONNECTOR_DOMAINS:
            if domain in ("system", "shortcuts"):
                # Ungated rows: always granted. shortcuts gets its own English
                # hint — the CLI needs no consent; a shortcut's own app
                # permissions are exercised inside the shortcut itself.
                status = STATUS_GRANTED
                hint = HINT_SHORTCUTS if domain == "shortcuts" else HINT_SYSTEM
            else:
                status = store_rows[domain]["status"]
                hint = _hint_for(status)
            connectors[domain] = {
                "status": status,
                "granted": status == STATUS_GRANTED,
                "last_probe": store_rows[domain]["last_probe"],
                "hint": hint,
            }
        return {"connectors": connectors}

    def run_action(self, domain: str, action: str, params: Any) -> dict:
        """Validate -> consent-gate -> argv-build -> run -> classify. Raises
        ConnectorError carrying the final user-visible message on failure."""
        if domain not in CONNECTOR_ACTIONS:
            raise ConnectorError(ERR_UNKNOWN_DOMAIN.format(domain=domain))
        if action not in CONNECTOR_ACTIONS[domain]:
            raise ConnectorError(
                ERR_UNKNOWN_ACTION.format(
                    domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
                )
            )
        if not isinstance(params, dict):
            raise ConnectorError(ERR_PARAMS_DICT)

        plan = self._build_argv(domain, action, params)

        # Shortcuts run: exact-existence pre-check BEFORE any run dispatch —
        # unknown names must produce a clean error, never CLI noise.
        if domain == "shortcuts" and action == "run":
            self._ensure_known_shortcut(params)

        if domain in GATED_DOMAINS and self.state.read()[domain]["status"] != STATUS_GRANTED:
            raise ConnectorError(
                f"{_DISPLAY_NAMES[domain]} ainda não está conectado — "
                f"conecte {domain} em Settings › Connectors."
            )

        try:
            outcome = self._execute(plan.cmd, timeout=plan.timeout, stdin=plan.stdin)
        except _ScriptFailure as failure:
            # Snapshot the PRE-failure consent state: the timeout copy split
            # keys on it (granted -> retry wording only; ungranted -> the
            # Settings/Connect consent hint). `shortcuts` is rows()-style
            # ungated (no consent flow exists for a CLI domain), so like a
            # granted domain it gets the retry wording — NEVER the consent
            # remediation — regardless of its stored state.
            pre_status = self.state.read()[domain]["status"]
            if failure.kind == "denied" and domain in GATED_DOMAINS:
                self.state.record(domain, STATUS_DENIED)
            granted = pre_status == STATUS_GRANTED or domain == "shortcuts"
            raise self._action_failure_error(
                domain, failure, granted=granted, timeout=plan.timeout
            ) from None

        result = self._normalize(domain, plan, outcome)

        # Success self-heal: a gated domain that just worked is granted.
        if domain in GATED_DOMAINS:
            current = self.state.read()[domain]["status"]
            if current != STATUS_GRANTED:
                self.state.record(domain, STATUS_GRANTED)
        return result

    def probe(self, domain: str) -> dict:
        """Single-flight per-domain probe; returns ``{ok, domain, status,
        last_probe}``. Never raises for app-level outcomes (a hang classifies
        needs_consent; a -1743/error classifies denied)."""
        if domain not in CONNECTOR_DOMAINS:
            raise ConnectorError(ERR_UNKNOWN_DOMAIN.format(domain=domain))
        lock = self._probe_locks[domain]
        if not lock.acquire(blocking=False):
            raise ProbeBusy(f"a probe for {domain!r} is already running")
        try:
            try:
                if domain in PROBE_COMMANDS:
                    # CLI domain: prove CLI availability by exit code (mirrors
                    # the `open` precedent — argv only, never shell).
                    cmd = PROBE_COMMANDS[domain]
                    timeout = SHORTCUTS_TIMEOUT_S
                else:
                    cmd = ["osascript", "-e", PROBE_SCRIPTS[domain]]
                    timeout = CONNECTORS_TIMEOUT_S
                self._execute(cmd, timeout=timeout)
                status = STATUS_GRANTED
            except _ScriptFailure as failure:
                status = STATUS_NEEDS_CONSENT if failure.kind == "timeout" else STATUS_DENIED
            stored = self.state.record(domain, status)
            return {"ok": True, "domain": domain, "status": status, "last_probe": stored["last_probe"]}
        finally:
            lock.release()

    # ---- internals -----------------------------------------------------------

    def _execute(
        self,
        cmd: List[str],
        timeout: float = CONNECTORS_TIMEOUT_S,
        stdin: Optional[str] = None,
    ) -> _Outcome:
        """One dispatch of the runner seam (mock-aware), normalized to _Outcome
        or classified as _ScriptFailure. Never returns a failing outcome."""
        if self._mock_mode:
            outcome = self._mock_runner(cmd, timeout, stdin)
            if outcome.returncode != 0:
                kind = "denied" if _is_denial_stderr(outcome.stderr) else "error"
                raise _ScriptFailure(kind, outcome.returncode, outcome.stderr)
            return outcome
        try:
            outcome = self._runner(cmd, timeout, stdin)
        except subprocess.TimeoutExpired as exc:
            raise _ScriptFailure("timeout", 0, "") from exc
        except OSError as exc:
            # Domain-neutral prefix: osascript, `open` and the shortcuts CLI
            # all dispatch through this seam (a missing binary must not read
            # as an osascript failure).
            raise ConnectorError(f"command execution failed: {exc}") from exc
        stdout = _as_runner_text(outcome.stdout)
        stderr = _as_runner_text(outcome.stderr)
        returncode = int(outcome.returncode)
        if returncode == 0:
            return _Outcome(returncode=0, stdout=stdout, stderr=stderr)
        kind = "denied" if _is_denial_stderr(stderr) else "error"
        raise _ScriptFailure(kind, returncode, stderr)

    @staticmethod
    def _action_failure_error(
        domain: str,
        failure: _ScriptFailure,
        granted: bool = False,
        timeout: float = CONNECTORS_TIMEOUT_S,
    ) -> ConnectorError:
        """Map a classified script failure to the FINAL user-visible message
        (PT-BR for the consent family; English for script errors). The timeout
        split is state-aware: a timeout on an ALREADY-granted domain is not a
        consent problem (first-use/warm-up/sync is the likely cause), so it
        gets retry wording with NO Settings/Connect redirect; the Settings
        consent hint is reserved for ungranted-state timeouts. ``timeout``
        threads the caller's real per-call timeout into the seconds rendering
        (CLI domains use SHORTCUTS_TIMEOUT_S; with the default every
        pre-existing domain's copy stays byte-identical)."""
        display = _DISPLAY_NAMES[domain]
        if failure.kind == "timeout":
            if granted:
                return ConnectorError(
                    f"{display} não respondeu a tempo — possível primeiro uso/sincronização; "
                    "tente de novo que eu reexecuto."
                )
            return ConnectorError(
                f"{display} não respondeu em {int(timeout)}s — pode ter aparecido "
                "um prompt de consentimento do macOS pendente (aprove-o com o aplicativo em "
                "foco) ou o app ainda está iniciando; tente de novo em instantes ou use o botão "
                "Connect em Settings › Connectors."
            )
        if failure.kind == "denied":
            return ConnectorError(
                f"O macOS negou a automação de {display} (-1743 / não autorizado). "
                f"Conecte {domain} em Settings › Connectors e aprove o prompt de consentimento "
                "com o aplicativo em foco."
            )
        if failure.kind == "error" and NO_ACCOUNT_MARKER in (failure.stderr or "").lower():
            return ConnectorError(
                "Nenhuma conta configurada — configure uma conta no Notas primeiro."
            )
        detail = _truncate_bytes(failure.stderr.strip(), 200) or "(sem stderr)"
        return ConnectorError(
            f"{display} action failed (exit {failure.returncode}): {detail}"
        )

    @staticmethod
    def _normalize(domain: str, plan: _ArgvPlan, outcome: _Outcome) -> dict:
        del domain  # reserved for per-domain normalization tweaks
        if plan.kind == "ok":
            return dict(plan.post({})) if plan.post is not None else {"ok": True}
        if plan.kind == "level_get":
            return {"level": _parse_level_stdout(outcome.stdout)}
        if plan.kind == "level_set":
            level = _parse_level_stdout(outcome.stdout)
            return {"ok": True, "level": level}
        if plan.kind == "playlist":
            return ConnectorManager._parse_playlist_stdout(outcome.stdout, plan.playlist_name)
        if plan.kind == "count":
            return {"ok": True, "deleted": _parse_count_stdout(outcome.stdout)}
        if plan.kind == "status":
            return _parse_status_stdout(outcome.stdout)
        if plan.kind == "shortcuts_list":
            return {"names": _split_lines(outcome.stdout)}
        if plan.kind == "shortcuts_run":
            # Success path is rc==0 (failures already classified in _execute);
            # stdout tail is byte-capped on a UTF-8 boundary.
            return {
                "output": _truncate_bytes(
                    (outcome.stdout or "").strip(), SHORTCUTS_OUTPUT_MAX_BYTES
                ),
                "exit_code": int(outcome.returncode),
            }
        if plan.kind == "unread":
            return {"unread": _parse_unread_stdout(outcome.stdout)}
        if plan.kind == "split_keep":
            # split_keep: raw .split keeps EMPTY field positions (contacts
            # rows have legitimately empty org/email/phone) — _split_delimited
            # would misalign them. Only osascript's result newline is trimmed
            # (rstrip("\r\n"), NOT .strip(): Python treats \x1f itself as
            # whitespace, and a plain strip would erase a row-final empty
            # field's trailing delimiter. osascript never emits \r, but
            # stripping both is free and a trailing "\x1f" survives intact —
            # trailing-empty-field semantics unchanged).
            assert plan.post is not None
            return plan.post({"items": (outcome.stdout or "").rstrip("\r\n").split(_DELIM)})
        if plan.kind == "send":
            # messages send: the frozen scripts RETURN the unknown-recipient
            # marker on rc-0 stdout (mirrored mail marker route).
            if (outcome.stdout or "").strip() == MESSAGES_UNKNOWN_RECIPIENT_MARKER:
                to = plan.cmd[3] if len(plan.cmd) > 3 else ""
                raise ConnectorError(ERR_UNKNOWN_RECIPIENT.format(to=to))
            return dict(plan.post({})) if plan.post is not None else {"ok": True}
        if plan.kind == "split":
            assert plan.post is not None
            return plan.post({"items": _split_delimited(outcome.stdout)})
        if plan.kind == "finder":
            # finder move/trash: the frozen scripts RETURN structured refusal
            # markers on rc-0 stdout (mirrored messages send route); the argv
            # positions map each marker back to its clean English copy.
            stdout = (outcome.stdout or "").strip()
            if stdout == FINDER_DST_DIR_MISSING_MARKER:
                raise ConnectorError(ERR_FINDER_DST_DIR_MISSING.format(dir=plan.cmd[4]))
            if stdout == FINDER_DST_EXISTS_MARKER:
                raise ConnectorError(ERR_FINDER_DST_EXISTS.format(name=plan.cmd[5]))
            if stdout == FINDER_NOT_FOUND_MARKER:
                raise ConnectorError(ERR_FINDER_NOT_FOUND.format(path=plan.cmd[3]))
            return dict(plan.post({})) if plan.post is not None else {"ok": True}
        if plan.kind == "complete":
            return ConnectorManager._parse_complete_stdout(outcome.stdout, plan.complete_query, plan.complete_list)
        if plan.kind == "read":
            return ConnectorManager._parse_read_stdout(outcome.stdout, plan.read_name)
        # "raw" — single trimmed text (front_app)
        text = (outcome.stdout or "").strip()
        if text == "":
            raise ConnectorError("the macOS app returned an empty result")
        return {"app": text}

    @staticmethod
    def _parse_complete_stdout(stdout: str, query: str, list_name: str) -> dict:
        if (stdout or "").strip() == "not_found":
            raise ConnectorError(
                f"Lembrete '{query}' não encontrado na lista '{list_name}' "
                "(verifique o nome exato e se ele ainda está pendente)."
            )
        items = _split_delimited(stdout)
        completed = items[1] if len(items) > 1 else query
        return {"ok": True, "completed": completed}

    @staticmethod
    def _parse_read_stdout(stdout: str, name: str) -> dict:
        if (stdout or "").strip() == "not_found":
            raise ConnectorError(
                f"Nota '{name}' não encontrada (a nota deve existir com esse nome exato)."
            )
        return {"name": name, "body": _truncate_bytes((stdout or "").strip(), READ_MAX_BYTES)}

    @staticmethod
    def _parse_playlist_stdout(stdout: str, name: str) -> dict:
        if (stdout or "").strip() == "not_found":
            raise ConnectorError(
                f"Playlist '{name}' não encontrada (verifique o nome exato)."
            )
        return {"ok": True, "playlist": name}

    # ---- mail stdout handling -------------------------------------------------

    @staticmethod
    def _mail_guard_items(items: List[str]) -> None:
        """Marker guard for mail's split-kind posts: a structured mail failure
        returns a BARE marker item instead of rows (single item), so any other
        list — including a legit one-message chunk — passes through safely."""
        if len(items) == 1:
            _raise_if_mail_marker(items[0].strip())

    # ---- argv builders (validate + freeze the exact command) ------------------

    def _build_argv(self, domain: str, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if domain == "reminders":
            return self._build_reminders(action, params)
        if domain == "notes":
            return self._build_notes(action, params)
        if domain == "calendar":
            return self._build_calendar(action, params)
        if domain == "music":
            return self._build_music(action, params)
        if domain == "shortcuts":
            return self._build_shortcuts(action, params)
        if domain == "mail":
            return self._build_mail(action, params)
        if domain == "contacts":
            return self._build_contacts(action, params)
        if domain == "messages":
            return self._build_messages(action, params)
        if domain == "finder":
            return self._build_finder(action, params)
        if domain == "quicktime":
            return self._build_quicktime(action, params)
        return self._build_system(action, params)

    def _build_reminders(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "list_lists":
            return _ArgvPlan(
                ["osascript", "-e", REMINDERS_LIST_LISTS],
                kind="split",
                post=lambda raw: {"lists": raw["items"]},
            )
        if action == "list_reminders":
            _required_params(params, ("list",))
            list_name = _as_string(params.get("list"), "list", LIST_NAME_MAX_CHARS)
            limit = _as_limit(params.get("limit"))
            return _ArgvPlan(
                ["osascript", "-e", REMINDERS_LIST_REMINDERS, list_name, str(limit)],
                kind="split",
                post=lambda raw: {"list": list_name, "reminders": raw["items"]},
            )
        if action == "add":
            _required_params(params, ("list", "title"))
            list_name = _as_string(params.get("list"), "list", LIST_NAME_MAX_CHARS)
            title = _as_string(params.get("title"), "title", TITLE_MAX_CHARS)
            due = _parse_due(params.get("due"))
            if due is None:
                argv = [list_name, title, "", "", ""]
                due_out: Optional[str] = None
            else:
                year, month, day = due
                argv = [list_name, title, str(year), f"{month:02d}", f"{day:02d}"]
                due_out = f"{year:04d}-{month:02d}-{day:02d}"

            def post(raw: dict, _list_name: str = list_name,
                     _title: str = title, _due: Optional[str] = due_out) -> dict:
                del raw
                result = {"ok": True, "list": _list_name, "title": _title}
                if _due is not None:
                    result["due"] = _due
                return result

            return _ArgvPlan(["osascript", "-e", REMINDERS_ADD, *argv], kind="ok", post=post)
        # complete — exactly one of title/id (both matched against reminder names)
        _required_params(params, ("list",))
        list_name = _as_string(params.get("list"), "list", LIST_NAME_MAX_CHARS)
        matched_field = _at_most_one(
            params,
            ("title", "id"),
            "complete requires exactly one of 'title' or 'id' (matched against reminder names)",
        )
        target = _as_string(params.get(matched_field), matched_field, TITLE_MAX_CHARS)
        return _ArgvPlan(
            ["osascript", "-e", REMINDERS_COMPLETE, list_name, target],
            kind="complete",
            complete_list=list_name,
            complete_query=target,
        )

    def _build_notes(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "search":
            _required_params(params, ("query",))
            query = _as_string(params.get("query"), "query", QUERY_MAX_CHARS)
            limit = _as_limit(params.get("limit"))
            return _ArgvPlan(
                ["osascript", "-e", NOTES_SEARCH, query, str(limit)],
                kind="split",
                post=lambda raw: {"notes": raw["items"]},
            )
        if action == "read":
            _required_params(params, ("name",))
            name = _as_string(params.get("name"), "name", LIST_NAME_MAX_CHARS)
            return _ArgvPlan(
                ["osascript", "-e", NOTES_READ, name],
                kind="read",
                read_name=name,
            )
        # create (default account/folder; title becomes the note name)
        _required_params(params, ("title",))
        title = _as_string(params.get("title"), "title", TITLE_MAX_CHARS)
        body = ""
        if params.get("body") not in (None, ""):
            body = _body_bytes_cap(params.get("body"), "body")

        def post(raw: dict, _title: str = title) -> dict:
            del raw
            return {"ok": True, "name": _title}

        return _ArgvPlan(["osascript", "-e", NOTES_CREATE, title, body], kind="ok", post=post)

    def _build_calendar(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "list_calendars":
            return _ArgvPlan(
                ["osascript", "-e", CALENDAR_LIST_CALENDARS],
                kind="split",
                post=lambda raw: {"calendars": raw["items"]},
            )
        if action == "list_events":
            calendar_name = ""
            if params.get("calendar") not in (None, ""):
                calendar_name = _as_string(params.get("calendar"), "calendar", LIST_NAME_MAX_CHARS)
            days = _as_days(params.get("days"))

            def post(raw: dict, _cal: str = calendar_name) -> dict:
                events = _chunk_rows(raw["items"], ("summary", "start", "end"))
                result = {"events": events}
                if _cal:
                    result["calendar"] = _cal
                return result

            return _ArgvPlan(
                ["osascript", "-e", CALENDAR_LIST_EVENTS, calendar_name, str(days)],
                kind="split",
                post=post,
            )
        if action == "add_event":
            _required_params(params, ("summary", "start", "end"))
            summary = _as_string(params.get("summary"), "summary", LIST_NAME_MAX_CHARS)
            start_y, start_m, start_d, start_h, start_mi = _parse_datetime_arg(params.get("start"), "start")
            end_y, end_m, end_d, end_h, end_mi = _parse_datetime_arg(params.get("end"), "end")
            calendar_name = ""
            if params.get("calendar") not in (None, ""):
                calendar_name = _as_string(params.get("calendar"), "calendar", LIST_NAME_MAX_CHARS)
            notes = ""
            if params.get("notes") not in (None, ""):
                notes = _as_string(params.get("notes"), "notes", TITLE_MAX_CHARS)
            alert = _as_positive_int(params.get("alert_minutes"), "alert_minutes")
            alert_text = "" if alert is None else str(alert)
            start_text = "%04d-%02d-%02d %02d:%02d" % (start_y, start_m, start_d, start_h, start_mi)

            def post(raw: dict, _summary: str = summary, _start: str = start_text,
                     _cal: str = calendar_name) -> dict:
                del raw
                result = {"ok": True, "summary": _summary, "start": _start}
                if _cal:
                    result["calendar"] = _cal
                return result

            return _ArgvPlan(
                [
                    "osascript", "-e", CALENDAR_ADD_EVENT,
                    summary,
                    str(start_y), "%02d" % start_m, "%02d" % start_d, "%02d" % start_h, "%02d" % start_mi,
                    str(end_y), "%02d" % end_m, "%02d" % end_d, "%02d" % end_h, "%02d" % end_mi,
                    calendar_name, notes, alert_text,
                ],
                kind="ok",
                post=post,
            )
        # delete_event — EXACT summary match, optional from-date + calendar
        _required_params(params, ("summary",))
        summary = _as_string(params.get("summary"), "summary", LIST_NAME_MAX_CHARS)
        from_date = _parse_date(params.get("from"), "from") if params.get("from") not in (None, "") else None
        calendar_name = ""
        if params.get("calendar") not in (None, ""):
            calendar_name = _as_string(params.get("calendar"), "calendar", LIST_NAME_MAX_CHARS)
        if from_date is None:
            from_argv = ["", "", ""]
        else:
            from_argv = [str(from_date[0]), "%02d" % from_date[1], "%02d" % from_date[2]]
        return _ArgvPlan(
            ["osascript", "-e", CALENDAR_DELETE_EVENT, summary, *from_argv, calendar_name],
            kind="count",
        )

    def _build_music(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "status":
            return _ArgvPlan(["osascript", "-e", MUSIC_STATUS], kind="status")
        if action in ("play", "pause", "next", "previous"):
            return _ArgvPlan(["osascript", "-e", SCRIPTS[("music", action)]], kind="ok")
        if action == "play_playlist":
            _required_params(params, ("name",))
            name = _as_string(params.get("name"), "name", QUERY_MAX_CHARS)
            return _ArgvPlan(
                ["osascript", "-e", MUSIC_PLAY_LIST, name],
                kind="playlist",
                playlist_name=name,
            )
        if action == "search_store":
            _required_params(params, ("query",))
            query = _as_string(params.get("query"), "query", QUERY_MAX_CHARS)
            return _ArgvPlan(
                ["osascript", "-e", MUSIC_SEARCH_STORE, query, "5"],
                kind="split",
                post=lambda raw: {"results": _chunk_rows(raw["items"], ("name", "artist", "album"))},
            )
        # volume (get/set split follows the system.volume precedent)
        if params.get("level") in (None, ""):
            return _ArgvPlan(["osascript", "-e", MUSIC_VOLUME_GET], kind="level_get")
        level = _as_level(params.get("level"))
        return _ArgvPlan(["osascript", "-e", MUSIC_VOLUME_SET, str(level)], kind="level_set")

    def _build_mail(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        # READ-LEVEL-1 only: counts, mailbox names, subject/from/date rows.
        # Bodies, send, delete, save and move are intentionally not
        # implemented (destructive/deferred — see module docstring).
        if action == "unread_count":
            mailbox = ""
            if "mailbox" in params:
                mailbox = _as_string(params["mailbox"], "mailbox", LIST_NAME_MAX_CHARS)
            account = ""
            if "account" in params:
                account = _as_string(params["account"], "account", LIST_NAME_MAX_CHARS)
            return _ArgvPlan(
                cmd=["osascript", "-e", MAIL_UNREAD_COUNT, mailbox, account],
                kind="unread",
            )
        if action == "mailboxes":
            def post(raw: dict) -> dict:
                ConnectorManager._mail_guard_items(raw["items"])
                return {"mailboxes": raw["items"]}

            return _ArgvPlan(
                cmd=["osascript", "-e", MAIL_MAILBOXES],
                kind="split",
                post=post,
            )
        if action == "list_recent":
            mailbox = ""
            if "mailbox" in params:
                mailbox = _as_string(params["mailbox"], "mailbox", LIST_NAME_MAX_CHARS)
            count = MAIL_LIST_MAX_COUNT
            if "count" in params:
                # _as_positive_int: None/"" -> None (absent). Above the cap
                # ERRORS (the _as_days precedent) — never clamped down.
                count_value = _as_positive_int(params["count"], "count")
                if count_value is not None:
                    if count_value > MAIL_LIST_MAX_COUNT:
                        raise ConnectorError(
                            f"count accepts at most {MAIL_LIST_MAX_COUNT} messages."
                        )
                    count = count_value

            def post(raw: dict) -> dict:
                ConnectorManager._mail_guard_items(raw["items"])
                return {"messages": _chunk_rows(raw["items"], ("subject", "from", "received"))}

            return _ArgvPlan(
                cmd=["osascript", "-e", MAIL_LIST_RECENT, mailbox, str(count)],
                kind="split",
                post=post,
            )
        raise ConnectorError(
            ERR_UNKNOWN_ACTION.format(
                domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
            )
        )

    @staticmethod
    def _validated_handle_list(
        params: Dict[str, Any], field: str, max_items: int,
        forbidden_substrings: Tuple[str, ...] = (),
    ) -> List[str]:
        """Validate an optional list of strings (contacts emails/phones):
        absent/None/"" -> []; non-list -> error; each item via _as_string;
        more than max_items ERRORS (never truncated). Each validated item must
        not contain a forbidden substring: the _DELIM case carries the
        dedicated delimiter copy (misaligned later split_keep/get parsing);
        anything else renders its repr (", " -> ', ': collide with
        CONTACTS_GET's ", "-joined list rendering)."""
        value = params.get(field)
        if value is None:
            return []
        if isinstance(value, str):
            if value.strip() == "":
                return []
            value = [value]  # tolerate a single bare string
        if not isinstance(value, list):
            raise ConnectorError(f"'{field}' must be a list of strings")
        items = [_as_string(item, field, LIST_NAME_MAX_CHARS) for item in value]
        for item in items:
            for forbidden in forbidden_substrings:
                if forbidden not in item:
                    continue
                if forbidden == _DELIM:
                    raise ConnectorError(
                        f"'{field}' must not contain the record delimiter character"
                    )
                raise ConnectorError(f"'{field}' must not contain {forbidden!r}")
        if len(items) > max_items:
            raise ConnectorError(f"'{field}' accepts at most {max_items} values.")
        return items

    def _build_contacts(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        # PII surface: search/get are read-only; create is gated by the same
        # consent; delete/update are intentionally NOT implemented.
        if action == "search":
            _required_params(params, ("query",))
            query = _as_string(params.get("query"), "query", QUERY_MAX_CHARS)
            limit = _optional_capped_int(
                params.get("limit"), "limit", LIST_LIMIT_MAX,
                CONTACTS_LIST_LIMIT_DEFAULT, "contacts",
            )

            def post(raw: dict) -> dict:
                # Raw-split input (rows may carry empty org/email/phone);
                # incomplete trailing rows dropped by _chunk_rows.
                return {
                    "contacts": _chunk_rows(
                        raw["items"], ("name", "org", "email", "phone")
                    )
                }

            return _ArgvPlan(
                ["osascript", "-e", CONTACTS_SEARCH, query, str(limit)],
                kind="split_keep",
                post=post,
            )
        if action == "get":
            _required_params(params, ("name",))
            name = _as_string(params.get("name"), "name", LIST_NAME_MAX_CHARS)

            def post(raw: dict, _name: str = name) -> dict:
                items = raw["items"]
                if len(items) == 1:
                    # Bare marker or empty stdout on rc 0 -> clean not-found.
                    raise ConnectorError(
                        'Contato não encontrado — use a ação "search" para '
                        "ver os nomes disponíveis."
                    )
                fields = (items + ["", "", "", "", ""])[:5]
                return {"contact": {
                    "name": fields[0],
                    "org": fields[1],
                    "emails": [e for e in fields[2].split(", ") if e != ""],
                    "phones": [p for p in fields[3].split(", ") if p != ""],
                    "notes": _truncate_bytes(fields[4], CONTACTS_NOTE_MAX_BYTES),
                }}

            return _ArgvPlan(
                ["osascript", "-e", CONTACTS_GET, name],
                kind="split_keep",
                post=post,
            )
        if action == "create":
            _required_params(params, ("name",))
            name = _as_string(params.get("name"), "name", LIST_NAME_MAX_CHARS)
            # Create-time delimiter guard: _DELIM anywhere in a STORED field
            # (name/org/emails/phones) would misalign later split_keep/get
            # parsing (middle-of-string survives _as_string.strip()). Read
            # paths (search query, get name, send to/text) never feed row
            # parsing and stay un-blocked.
            if _DELIM in name:
                raise ConnectorError(
                    "'name' must not contain the record delimiter character"
                )
            org = ""
            if params.get("org") not in (None, ""):
                org = _as_string(params.get("org"), "org", LIST_NAME_MAX_CHARS)
                if _DELIM in org:
                    raise ConnectorError(
                        "'org' must not contain the record delimiter character"
                    )
            # emails also reject ", " (CONTACTS_GET joins its email/phone lists
            # with ", " — a stored collision would split into phantom lists).
            emails = self._validated_handle_list(
                params, "emails", CONTACTS_EMAILS_MAX_COUNT,
                forbidden_substrings=(", ", _DELIM),
            )
            phones = self._validated_handle_list(
                params, "phones", CONTACTS_PHONES_MAX_COUNT,
                forbidden_substrings=(", ", _DELIM),
            )
            email_argv = emails + [""] * (CONTACTS_EMAILS_MAX_COUNT - len(emails))
            phone_argv = phones + [""] * (CONTACTS_PHONES_MAX_COUNT - len(phones))

            def post(raw: dict, _name: str = name) -> dict:
                del raw
                return {"ok": True, "created": _name}

            return _ArgvPlan(
                ["osascript", "-e", CONTACTS_CREATE, name, org,
                 *email_argv, *phone_argv],
                kind="ok",
                post=post,
            )
        raise ConnectorError(
            ERR_UNKNOWN_ACTION.format(
                domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
            )
        )

    def _build_messages(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        # SEND-ONLY: listing threads exposes handle + last-message DATE ONLY
        # (never text). A recipient that is neither a raw e-mail nor a +E.164
        # number rides the contact-lookup script, which refuses unknown names.
        if action == "send_message":
            _required_params(params, ("to", "text"))
            to = _as_string(params.get("to"), "to", MESSAGES_TO_MAX_CHARS)
            text = _as_string(params.get("text"), "text", MESSAGES_TEXT_MAX_CHARS)
            if _is_email_handle(to) or _is_e164_handle(to):
                script = MESSAGES_SEND_HANDLE
            else:
                script = MESSAGES_SEND_CONTACT

            def post(raw: dict, _to: str = to) -> dict:
                del raw
                return {"ok": True, "sent": True, "to": _to}

            return _ArgvPlan(
                ["osascript", "-e", script, to, text],
                kind="send",
                post=post,
            )
        if action == "list_recent_threads":
            count = _optional_capped_int(
                params.get("count"), "count", MESSAGES_THREADS_MAX_COUNT,
                MESSAGES_THREADS_MAX_COUNT, "threads",
            )

            def post(raw: dict) -> dict:
                # Privacy floor: handle + date ONLY — message text is absent
                # from the script output entirely.
                return {
                    "threads": _chunk_rows(raw["items"], ("handle", "date"))
                }

            return _ArgvPlan(
                ["osascript", "-e", MESSAGES_LIST_THREADS, str(count)],
                kind="split_keep",
                post=post,
            )
        raise ConnectorError(
            ERR_UNKNOWN_ACTION.format(
                domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
            )
        )

    def _build_finder(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        # File-surface policy: paths are validated server-side against the
        # allowed roots (expanduser + realpath + prefix compare); trash is
        # NOT delete (Finder `delete` moves to Trash); no overwrite on move.
        if action == "list":
            if params.get("path") in (None, ""):
                path = os.path.realpath(os.path.expanduser("~/Desktop"))
            else:
                path = _resolve_under_allowed_root(
                    _as_string(params.get("path"), "path", FINDER_PATH_MAX_CHARS), "path"
                )

            def post(raw: dict, _path: str = path) -> dict:
                items = raw["items"]
                if items and items[0] == FINDER_NOT_FOUND_MARKER:
                    raise ConnectorError(ERR_FINDER_NO_FOLDER.format(path=_path))
                return {"items": items[:FINDER_LIST_MAX_ITEMS]}

            return _ArgvPlan(
                ["osascript", "-e", FINDER_LIST, path],
                kind="split",
                post=post,
            )
        if action == "reveal":
            path = _resolve_under_allowed_root(
                _as_string(params.get("path"), "path", FINDER_PATH_MAX_CHARS), "path"
            )

            def post(raw: dict, _path: str = path) -> dict:
                del raw
                return {"ok": True, "path": _path}

            # argv-only (system.open precedent): ["open", "-R", path], no osascript.
            return _ArgvPlan(["open", "-R", path], kind="ok", post=post)
        if action == "info":
            path = _resolve_under_allowed_root(
                _as_string(params.get("path"), "path", FINDER_PATH_MAX_CHARS), "path"
            )

            def post(raw: dict, _path: str = path) -> dict:
                items = raw["items"]
                if items and items[0] == FINDER_NOT_FOUND_MARKER:
                    raise ConnectorError(ERR_FINDER_NOT_FOUND.format(path=_path))
                rows = _chunk_rows(items, ("kind", "size", "extension", "created", "modified"))
                if not rows:
                    raise ConnectorError(ERR_FINDER_NOT_FOUND.format(path=_path))
                return {"info": rows[0]}

            return _ArgvPlan(
                ["osascript", "-e", FINDER_INFO, path],
                kind="split_keep",
                post=post,
            )
        if action == "move":
            _required_params(params, ("src", "dst_dir"))
            src = _resolve_under_allowed_root(
                _as_string(params.get("src"), "src", FINDER_PATH_MAX_CHARS), "src"
            )
            dst_dir = _resolve_under_allowed_root(
                _as_string(params.get("dst_dir"), "dst_dir", FINDER_PATH_MAX_CHARS), "dst_dir"
            )
            name = os.path.basename(src)

            def post(raw: dict, _src: str = src, _dst_dir: str = dst_dir, _name: str = name) -> dict:
                del raw
                return {"ok": True, "moved": True, "from": _src, "to": os.path.join(_dst_dir, _name)}

            return _ArgvPlan(
                ["osascript", "-e", FINDER_MOVE, src, dst_dir, name],
                kind="finder",
                post=post,
            )
        if action == "trash":
            path = _resolve_trash_path(
                _as_string(params.get("path"), "path", FINDER_PATH_MAX_CHARS)
            )

            def post(raw: dict, _path: str = path) -> dict:
                del raw
                return {"ok": True, "trashed": _path}

            return _ArgvPlan(
                ["osascript", "-e", FINDER_TRASH, path],
                kind="finder",
                post=post,
            )
        raise ConnectorError(
            ERR_UNKNOWN_ACTION.format(
                domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
            )
        )

    def _build_quicktime(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        # Recording slot: ONE at a time (process memory; lost on restart —
        # safe). start/new_audio refuse a second recording BEFORE the gate.
        if action == "start_recording":
            audio = params.get("with_audio")
            if audio is None:
                audio = False
            if not isinstance(audio, bool):
                raise ConnectorError("'with_audio' must be true or false")
            if self._qt_recording is not None:
                raise ConnectorError(
                    ERR_QT_ALREADY_RECORDING.format(doc_id=self._qt_recording["doc_id"])
                )

            def post(raw: dict, _audio: bool = audio) -> dict:
                items = raw["items"]
                doc_id = items[0] if items else ""
                if doc_id == QUICKTIME_DOC_TIMEOUT_MARKER:
                    # `new screen recording` opened the setup window but no
                    # document appeared within 30s (Setup needs a manual
                    # "Record" click on modern macOS — no UI scripting here).
                    # Do NOT occupy the recording slot.
                    raise ConnectorError(
                        ERR_QT_RECORD_SETUP.format(display=_DISPLAY_NAMES["quicktime"])
                    )
                self._qt_recording = {"doc_id": doc_id, "kind": "screen"}
                return {"ok": True, "doc_id": doc_id, "with_audio": _audio}

            return _ArgvPlan(
                ["osascript", "-e", QUICKTIME_START_SCREEN, "1" if audio else "0"],
                kind="split_keep",
                post=post,
                timeout=QUICKTIME_RECORD_TIMEOUT_S,
            )
        if action == "new_audio_recording":
            if self._qt_recording is not None:
                raise ConnectorError(
                    ERR_QT_ALREADY_RECORDING.format(doc_id=self._qt_recording["doc_id"])
                )

            def post(raw: dict) -> dict:
                items = raw["items"]
                doc_id = items[0] if items else ""
                self._qt_recording = {"doc_id": doc_id, "kind": "audio"}
                return {"ok": True, "doc_id": doc_id}

            return _ArgvPlan(
                ["osascript", "-e", QUICKTIME_NEW_AUDIO],
                kind="split_keep",
                post=post,
                timeout=QUICKTIME_RECORD_TIMEOUT_S,
            )
        if action == "stop_recording":
            recording = self._qt_recording
            if recording is None:
                raise ConnectorError(ERR_NO_RECORDING)
            doc_id = recording["doc_id"]

            def post(raw: dict, _doc_id: str = doc_id) -> dict:
                items = raw["items"]
                # Release the slot FIRST: saved or doc-gone, the process-side
                # recording is over either way.
                self._qt_recording = None
                if items and items[0] == QUICKTIME_DOC_GONE_MARKER:
                    raise ConnectorError(ERR_QT_DOC_GONE.format(doc_id=_doc_id))
                path = items[0] if items else ""
                return {"ok": True, "path": path}

            return _ArgvPlan(
                ["osascript", "-e", QUICKTIME_STOP, doc_id],
                kind="split_keep",
                post=post,
            )
        if action == "play":
            file_path = _safe_expand_resolved(
                _as_string(params.get("file"), "file", QUICKTIME_FILE_MAX_CHARS), "file"
            )
            if not os.path.isfile(file_path):
                raise ConnectorError(ERR_QT_PLAY_FILE.format(path=file_path))

            def post(raw: dict, _path: str = file_path) -> dict:
                del raw
                return {"ok": True, "path": _path}

            return _ArgvPlan(
                ["osascript", "-e", QUICKTIME_PLAY, file_path],
                kind="ok",
                post=post,
            )
        raise ConnectorError(
            ERR_UNKNOWN_ACTION.format(
                domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
            )
        )

    def _build_shortcuts(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        # CLI domain: no osascript source — argv lists start with "shortcuts"
        # (mirror of `open` in _build_system). `-i FILE` is intentionally not
        # whitelisted; text input rides on stdin (`-x` + subprocess input=).
        #
        # Known limitations (redteam-flagged, intentional, NOT changed):
        # - names starting with "-" are not rejected — like the music
        #   play_playlist precedent they ride argv verbatim and may be
        #   misparsed as CLI flags by the real binary;
        # - every `run` additionally pays ONE extra `shortcuts list`
        #   subprocess (the pre-check in _ensure_known_shortcut,
        #   <=SHORTCUTS_TIMEOUT_S each).
        if action == "list":
            return _ArgvPlan(
                cmd=["shortcuts", "list"],
                kind="shortcuts_list",
                timeout=SHORTCUTS_TIMEOUT_S,
            )
        if action == "run":
            _required_params(params, ("name",))
            name = _as_string(params["name"], "name", LIST_NAME_MAX_CHARS)
            input_text = None
            if "input_text" in params:
                input_text = _as_string(
                    params["input_text"], "input_text", SHORTCUTS_INPUT_MAX_CHARS
                )
            if "stdin" in params:
                if params["stdin"] != "text":
                    raise ConnectorError('stdin only accepts the value "text".')
                if input_text is None:
                    raise ConnectorError('stdin="text" requires an input_text param.')
            extra_argv = ["-x"] if input_text is not None else []
            return _ArgvPlan(
                cmd=["shortcuts", "run", name] + extra_argv,
                kind="shortcuts_run",
                timeout=SHORTCUTS_TIMEOUT_S,
                stdin=input_text,
            )
        raise ConnectorError(
            ERR_UNKNOWN_ACTION.format(
                domain=domain, action=action, actions=", ".join(CONNECTOR_ACTIONS[domain])
            )
        )

    def _ensure_known_shortcut(self, params: Dict[str, Any]) -> None:
        """Mission: unknown shortcut names must produce a clean error, never
        CLI noise. Exact-existence match against `shortcuts list` stdout lines
        BEFORE any run dispatch (runner.calls never contains the run cmd).

        Cost/limits (redteam-flagged, intentional): every `run` pays ONE extra
        `shortcuts list` subprocess (<=SHORTCUTS_TIMEOUT_S each); the check is
        a snapshot, so a name that vanishes between listing and running
        (TOCTOU) surfaces through the generic rc!=0 failure fallback instead.
        Dash-prefixed names are NOT rejected — like the music play_playlist
        precedent they ride argv verbatim and may be misparsed as CLI flags by
        the real binary."""
        name = _as_string(params["name"], "name", LIST_NAME_MAX_CHARS)
        try:
            listing = self._execute(["shortcuts", "list"], timeout=SHORTCUTS_TIMEOUT_S)
        except _ScriptFailure as failure:
            # granted=True: the CLI cold-start wording fits an UNGATED domain
            # (the consent-hint variant would point at Settings › Connectors,
            # which does not apply to shortcuts); the 10 s CLI timeout is
            # threaded for the seconds rendering.
            raise self._action_failure_error(
                "shortcuts", failure, granted=True, timeout=SHORTCUTS_TIMEOUT_S
            ) from None
        known = _split_lines(listing.stdout)
        if name not in known:
            raise ConnectorError(
                'Atalho não encontrado — use a ação "list" para ver os atalhos '
                "disponíveis."
            )

    def _build_system(self, action: str, params: Dict[str, Any]) -> _ArgvPlan:
        if action == "volume":
            if params.get("level") in (None, ""):
                return _ArgvPlan(["osascript", "-e", SYSTEM_VOLUME_GET], kind="level_get")
            level = _as_level(params.get("level"))
            return _ArgvPlan(["osascript", "-e", SYSTEM_VOLUME_SET, str(level)], kind="level_set")
        if action == "front_app":
            return _ArgvPlan(["osascript", "-e", SYSTEM_FRONT_APP], kind="raw")
        if action == "open":
            # open runs ["open", ...] directly (NO osascript), validated hard:
            # http(s) URLs -> plain open; app names -> open -a; rest rejected.
            target = _as_string(params.get("target"), "target", 1024)
            if _URL_TARGET_RE.match(target):
                cmd = ["open", target]
            elif _APP_TARGET_RE.match(target) and not target.startswith("-"):
                cmd = ["open", "-a", target]
            else:
                raise ConnectorError(
                    "invalid open target: only http(s) URLs or app names "
                    "(letters, digits, space, '.', '_', '-', max 64 chars, "
                    "never starting with '-') are allowed"
                )

            def post(raw: dict, _target: str = target) -> dict:
                del raw
                return {"ok": True, "target": _target}

            return _ArgvPlan(cmd, kind="ok", post=post)
        # toast
        title = _as_string(params.get("title"), "title", TITLE_MAX_CHARS)
        body = _truncate_bytes(_as_string(params.get("body"), "body"), BODY_MAX_BYTES)

        def post(raw: dict, _title: str = title) -> dict:
            del raw
            return {"ok": True, "title": _title}

        return _ArgvPlan(["osascript", "-e", SYSTEM_TOAST, title, body], kind="ok", post=post)

    # ---- mock world ------------------------------------------------------------

    def reset_mock_world(self) -> None:
        """Reset the in-memory mock world (test/smoke convenience)."""
        with self._mock_lock:
            self._mock_lists = {}
            self._mock_notes = []
            self._mock_volume = 50
            self._mock_front_app = "Finder"
            self._mock_last_toast = None
            self._mock_calendars = ["Pessoal", "Trabalho"]
            self._mock_events = []
            self._mock_music = {
                "state": "paused", "name": "", "artist": "", "album": "",
                "position": 0, "duration": 0, "volume": 50,
                "playlists": ["Foco", "Treino"],
            }
            self._mock_shortcuts = {
                "names": ["Resumo da semana", "Abrir agenda", "Backup das fotos"],
            }
            self._mock_mail = {
                "unread": 3,
                "accounts": ["iCloud", "Gmail"],
                "mailboxes": ["INBOX", "Enviados", "Arquivo"],
                "recent": [
                    ("Relatório mensal pronto", "Fulano <fulano@exemplo.com>", "31/10/2026 18:02"),
                    ("Re: contratos", "Beltrano <beltrano@exemplo.com>", "01/11/2026 09:15"),
                ],
            }
            self._mock_contacts = {
                "people": [
                    {"name": "Marina Duarte", "org": "Imaginária Ltda",
                     "emails": ["marina@imaginaria.test"],
                     "phones": ["+5511922220001"], "note": "Prefere contato por e-mail."},
                    {"name": "Bruno Tanaka", "org": "",
                     "emails": ["bruno@tanaka.test"], "phones": [], "note": ""},
                ],
            }
            self._mock_threads = [
                ("marina@imaginaria.test", "2026-11-07"),
                ("+5511922220001", "2026-11-06"),
            ]
            # FASE 4 seeds (finder + quicktime) — identical to __init__.
            _desktop = os.path.realpath(os.path.expanduser("~/Desktop"))
            _downloads = os.path.realpath(os.path.expanduser("~/Downloads"))
            self._mock_finder = {
                "folders": {
                    _desktop: ["Relatório.pdf", "Captura de Tela.png", "trabalho"],
                    _downloads: ["Relatório.pdf"],
                },
                "items": {
                    os.path.join(_desktop, "Relatório.pdf"): {
                        "kind": "PDF document", "size": "20480", "extension": "pdf",
                        "created": "31/10/2026 18:02", "modified": "01/11/2026 09:15",
                    },
                    os.path.join(_desktop, "Captura de Tela.png"): {
                        "kind": "PNG image", "size": "512000", "extension": "png",
                        "created": "01/11/2026 08:00", "modified": "01/11/2026 08:00",
                    },
                    os.path.join(_desktop, "trabalho"): {
                        "kind": "Folder", "size": "", "extension": "",
                        "created": "30/10/2026 12:00", "modified": "02/11/2026 10:30",
                    },
                    os.path.join(_downloads, "Relatório.pdf"): {
                        "kind": "PDF document", "size": "1024", "extension": "pdf",
                        "created": "02/11/2026 10:00", "modified": "02/11/2026 10:00",
                    },
                },
            }
            self._mock_qt_seq = 0

    def _mock_runner(self, cmd: List[str], timeout: float, stdin: Optional[str] = None) -> _Outcome:
        """In-process osascript/CLI replacement: interprets the SAME frozen
        scripts against the in-memory world (add -> list stays genuinely
        observable). Equality-compares cmd[2] against the constants; the
        shortcuts CLI domain branches on cmd[1] BEFORE that lookup (its cmd[2]
        is the shortcut NAME, not a script)."""
        del timeout, stdin
        script = cmd[2] if len(cmd) > 2 else ""
        argv = list(cmd[3:])
        with self._mock_lock:
            # CLI domain first: cmd[2] of a shortcuts run is the shortcut
            # NAME, not a script constant — branch on cmd[1] BEFORE the
            # osascript script lookup.
            if cmd[0] == "shortcuts":
                if cmd[1] == "list":
                    # Real CLI prints one name per line, newline-terminated.
                    return _Outcome(0, "\n".join(self._mock_shortcuts["names"]) + "\n", "")
                if cmd[1] == "run":
                    return _Outcome(0, "atalho executado", "")
            if script == REMINDERS_ADD:
                list_name, title, year, month, day = (argv + [""] * 5)[:5]
                bucket = self._mock_lists.setdefault(list_name, [])
                bucket.append(
                    {
                        "title": title,
                        "completed": False,
                        "due": None if year == "" else "%s-%s-%s" % (year, month, day),
                    }
                )
                return _Outcome(0, "ok", "")
            if script == REMINDERS_COMPLETE:
                list_name, target = argv[:2]
                for item in self._mock_lists.get(list_name, []):
                    if not item["completed"] and item["title"].lower() == target.lower():
                        item["completed"] = True
                        return _Outcome(0, "ok" + _DELIM + item["title"], "")
                return _Outcome(0, "not_found", "")
            if script == REMINDERS_LIST_LISTS:
                return _Outcome(0, _DELIM.join(sorted(self._mock_lists.keys())), "")
            if script == REMINDERS_LIST_REMINDERS:
                list_name, limit_text = argv[:2]
                names = [
                    item["title"]
                    for item in self._mock_lists.get(list_name, [])
                    if not item["completed"]
                ][: int(limit_text)]
                return _Outcome(0, _DELIM.join(names), "")
            if script == NOTES_SEARCH:
                query, limit_text = argv[:2]
                needle = query.lower()
                names = [
                    note["name"]
                    for note in self._mock_notes
                    if needle in note["name"].lower()
                ][: int(limit_text)]
                return _Outcome(0, _DELIM.join(names), "")
            if script == NOTES_READ:
                name = argv[0]
                for note in self._mock_notes:
                    if note["name"].lower() == name.lower():
                        return _Outcome(0, note["body"], "")
                return _Outcome(0, "not_found", "")
            if script == NOTES_CREATE:
                title, body = (argv + [""])[:2]
                self._mock_notes.append({"name": title, "body": body if body else title})
                return _Outcome(0, "ok", "")
            if script == SYSTEM_VOLUME_GET:
                return _Outcome(0, str(self._mock_volume), "")
            if script == SYSTEM_VOLUME_SET:
                self._mock_volume = int(argv[0])
                return _Outcome(0, str(self._mock_volume), "")
            if script == SYSTEM_FRONT_APP:
                return _Outcome(0, self._mock_front_app, "")
            if script == SYSTEM_TOAST:
                self._mock_last_toast = (argv[0], argv[1] if len(argv) > 1 else "")
                return _Outcome(0, "ok", "")
            if script == CALENDAR_LIST_CALENDARS:
                return _Outcome(0, _DELIM.join(self._mock_calendars), "")
            if script == CALENDAR_LIST_EVENTS:
                _cal, _days = argv[0], int(argv[1])
                now = datetime.now()
                horizon = now + timedelta(days=_days)
                rows = []
                for ev in self._mock_events:
                    start = datetime.strptime(ev["start"], "%Y-%m-%d %H:%M")
                    # mock simplification: events added with an empty calendar
                    # only list under the all-calendars view.
                    if _cal in ("", ev["calendar"]) and now <= start <= horizon:
                        rows += [ev["summary"], ev["start"].replace(" ", "T"),
                                 ev["end"].replace(" ", "T")]
                return _Outcome(0, _DELIM.join(rows), "")
            if script == CALENDAR_ADD_EVENT:
                parts = (argv + [""] * 14)[:14]
                summary, calendar_name, notes, alert = parts[0], parts[11], parts[12], parts[13]
                if calendar_name and calendar_name not in self._mock_calendars:
                    return _Outcome(
                        1, "",
                        'execution error: Calendar got an error: Can\'t get calendar "%s".'
                        % calendar_name,
                    )
                self._mock_events.append({
                    "summary": summary, "start": "%s-%s-%s %s:%s" % (parts[1], parts[2], parts[3], parts[4], parts[5]),
                    "end": "%s-%s-%s %s:%s" % (parts[6], parts[7], parts[8], parts[9], parts[10]),
                    "calendar": calendar_name, "notes": notes, "alert": alert,
                })
                return _Outcome(0, "ok", "")
            if script == CALENDAR_DELETE_EVENT:
                parts = (argv + [""] * 5)[:5]
                summary, calendar_name = parts[0], parts[4]
                if calendar_name and calendar_name not in self._mock_calendars:
                    return _Outcome(
                        1, "",
                        'execution error: Calendar got an error: Can\'t get calendar "%s".'
                        % calendar_name,
                    )
                from_dt = None
                if parts[1]:
                    from_dt = datetime.strptime(
                        "%s-%s-%s 00:00" % (parts[1], parts[2], parts[3]), "%Y-%m-%d %H:%M"
                    )
                keep, deleted = [], 0
                for ev in self._mock_events:
                    if ev["summary"] != summary:  # EXACT match
                        keep.append(ev)
                        continue
                    if calendar_name and ev["calendar"] != calendar_name:
                        keep.append(ev)
                        continue
                    if from_dt is not None:
                        if datetime.strptime(ev["start"], "%Y-%m-%d %H:%M") < from_dt:
                            keep.append(ev)
                            continue
                    deleted += 1
                self._mock_events = keep
                return _Outcome(0, str(deleted), "")
            if script == MUSIC_STATUS:
                m = self._mock_music
                return _Outcome(0, _DELIM.join([
                    m["state"], m["name"], m["artist"], m["album"],
                    str(m["position"]), str(m["duration"]),
                ]), "")
            if script == MUSIC_PLAY:
                self._mock_music["state"] = "playing"
                return _Outcome(0, "ok", "")
            if script == MUSIC_PAUSE:
                self._mock_music["state"] = "paused"
                return _Outcome(0, "ok", "")
            if script in (MUSIC_NEXT, MUSIC_PREVIOUS):
                return _Outcome(0, "ok", "")
            if script == MUSIC_PLAY_LIST:
                playlists = self._mock_music["playlists"]
                if argv and argv[0] in playlists:
                    self._mock_music["state"] = "playing"
                    # playlist playback surfaces as a track with the playlist name
                    self._mock_music["name"] = argv[0]
                    return _Outcome(0, "ok", "")
                return _Outcome(0, "not_found", "")
            if script == MUSIC_SEARCH_STORE:
                rows = ["Track A", "Artist A", "Album A", "Track B", "Artist B", "Album B"]
                return _Outcome(0, _DELIM.join(rows), "")
            if script == MUSIC_VOLUME_GET:
                return _Outcome(0, str(self._mock_music["volume"]), "")
            if script == MUSIC_VOLUME_SET:
                self._mock_music["volume"] = int(argv[0])
                return _Outcome(0, str(self._mock_music["volume"]), "")
            if script == MAIL_UNREAD_COUNT:
                mailbox = argv[0] if argv else ""
                account = argv[1] if len(argv) > 1 else ""
                # Account check mirrors the script's named-account pre-check;
                # a known account proceeds with the mailbox-name rule.
                if account and account not in self._mock_mail["accounts"]:
                    return _Outcome(0, NO_ACCOUNT_MARKER, "")
                if mailbox and mailbox not in self._mock_mail["mailboxes"]:
                    return _Outcome(0, MAIL_MISSING_MAILBOX_MARKER, "")
                return _Outcome(0, str(self._mock_mail["unread"]), "")
            if script == MAIL_MAILBOXES:
                return _Outcome(0, _DELIM.join(self._mock_mail["mailboxes"]), "")
            if script == MAIL_LIST_RECENT:
                # NOTE: no account channel here — the script signature is
                # plan-verbatim {mailboxArg, limitArg} and the builder
                # whitelist carries no `account` param for list_recent, so
                # argv[1] is the COUNT, never an account name.
                mailbox = argv[0] if argv else ""
                count = int(argv[1]) if len(argv) > 1 else MAIL_LIST_MAX_COUNT
                if mailbox and mailbox not in self._mock_mail["mailboxes"]:
                    return _Outcome(0, MAIL_MISSING_MAILBOX_MARKER, "")
                rows = []
                for subject, sender, received in self._mock_mail["recent"][:count]:
                    rows += [subject, sender, received]
                return _Outcome(0, _DELIM.join(rows), "")
            if script == CONTACTS_SEARCH:
                query = argv[0] if argv else ""
                lim = int(argv[1]) if len(argv) > 1 else CONTACTS_LIST_LIMIT_DEFAULT
                needle = query.lower()
                rows2 = []
                for person in self._mock_contacts["people"]:
                    # Matching mirrors the frozen script: name `contains`, case-insensitive.
                    haystack = person["name"].lower()
                    if needle not in haystack:
                        continue
                    rows2 += [
                        person["name"], person.get("org", ""),
                        person["emails"][0] if person["emails"] else "",
                        person["phones"][0] if person["phones"] else "",
                    ]
                    if len(rows2) >= lim * 4:
                        break
                return _Outcome(0, _DELIM.join(rows2), "")
            if script == CONTACTS_GET:
                name = argv[0] if argv else ""
                match = None
                for person in self._mock_contacts["people"]:
                    if person["name"] == name:  # EXACT match
                        match = person
                        break
                if match is None:
                    return _Outcome(0, CONTACTS_NOT_FOUND_MARKER, "")
                return _Outcome(0, _DELIM.join([
                    match["name"], match.get("org", ""),
                    ", ".join(match["emails"]), ", ".join(match["phones"]),
                    match.get("note", ""),
                ]), "")
            if script == CONTACTS_CREATE:
                parts = (argv + [""] * 8)[:8]
                self._mock_contacts["people"].append({
                    "name": parts[0], "org": parts[1],
                    "emails": [e for e in parts[2:5] if e != ""],
                    "phones": [p for p in parts[5:8] if p != ""],
                    "note": "",
                })
                return _Outcome(0, "created", "")
            if script == MESSAGES_SEND_HANDLE:
                return _Outcome(0, "sent", "")
            if script == MESSAGES_SEND_CONTACT:
                # Mirrors the frozen script's send guard: count EXACT-name
                # matches; >1 is ambiguous -> refuse (never guess a recipient);
                # exactly one resolves first email, else first phone, else
                # nothing sendable -> refusal marker.
                to_name = argv[0] if argv else ""
                exact = [p for p in self._mock_contacts["people"] if p["name"] == to_name]
                if len(exact) == 1:
                    handle = exact[0]["emails"][0] if exact[0]["emails"] else ""
                    if handle == "":
                        handle = exact[0]["phones"][0] if exact[0]["phones"] else ""
                    if handle == "":
                        return _Outcome(0, MESSAGES_UNKNOWN_RECIPIENT_MARKER, "")
                    return _Outcome(0, "sent", "")
                return _Outcome(0, MESSAGES_UNKNOWN_RECIPIENT_MARKER, "")
            if script == MESSAGES_LIST_THREADS:
                lim = int(argv[0]) if argv else MESSAGES_THREADS_MAX_COUNT
                rows3 = []
                for handle, date in self._mock_threads[:lim]:
                    rows3 += [handle, date]
                return _Outcome(0, _DELIM.join(rows3), "")
            if script == FINDER_LIST:
                _folder_path = argv[0] if argv else ""
                children = self._mock_finder["folders"].get(_folder_path)
                if children is None:
                    # Mirror the script: folder coercion of a missing or
                    # non-folder path errors -> not-found marker.
                    return _Outcome(0, FINDER_NOT_FOUND_MARKER, "")
                return _Outcome(0, _DELIM.join(children[:FINDER_LIST_MAX_ITEMS]), "")
            if script == FINDER_INFO:
                _item_path = argv[0] if argv else ""
                _item = self._mock_finder["items"].get(_item_path)
                if _item is None:
                    return _Outcome(0, FINDER_NOT_FOUND_MARKER, "")
                return _Outcome(0, _DELIM.join([
                    _item["kind"], _item["size"], _item["extension"],
                    _item["created"], _item["modified"],
                ]), "")
            if script == FINDER_MOVE:
                _src = argv[0] if argv else ""
                _dst_dir = argv[1] if len(argv) > 1 else ""
                _name = argv[2] if len(argv) > 2 else ""
                if _dst_dir not in self._mock_finder["folders"]:
                    return _Outcome(0, FINDER_DST_DIR_MISSING_MARKER, "")
                if _name in self._mock_finder["folders"][_dst_dir]:
                    return _Outcome(0, FINDER_DST_EXISTS_MARKER, "")
                if _src not in self._mock_finder["items"]:
                    return _Outcome(0, FINDER_NOT_FOUND_MARKER, "")
                _parent = os.path.dirname(_src)
                if _parent in self._mock_finder["folders"] and _name in self._mock_finder["folders"][_parent]:
                    self._mock_finder["folders"][_parent].remove(_name)
                self._mock_finder["folders"].setdefault(_dst_dir, []).append(_name)
                self._mock_finder["items"][os.path.join(_dst_dir, _name)] = self._mock_finder["items"].pop(_src)
                return _Outcome(0, "moved", "")
            if script == FINDER_TRASH:
                _item_path = argv[0] if argv else ""
                _trashed = self._mock_finder["items"].pop(_item_path, None)
                if _trashed is None:
                    return _Outcome(0, FINDER_NOT_FOUND_MARKER, "")
                _parent = os.path.dirname(_item_path)
                _name = os.path.basename(_item_path)
                if _parent in self._mock_finder["folders"] and _name in self._mock_finder["folders"][_parent]:
                    self._mock_finder["folders"][_parent].remove(_name)
                return _Outcome(0, "trashed", "")
            if script == QUICKTIME_START_SCREEN:
                self._mock_qt_seq += 1
                return _Outcome(0, "QT-MOCK-%d\n" % self._mock_qt_seq, "")
            if script == QUICKTIME_NEW_AUDIO:
                self._mock_qt_seq += 1
                return _Outcome(0, "QT-MOCK-%d\n" % self._mock_qt_seq, "")
            # Doc-NAME semantics + doc-gone mirror: a stop whose argv name
            # matches no current slot document returns the SAME marker the
            # frozen script returns when the remembered document is gone.
            if script == QUICKTIME_STOP:
                _doc_name = argv[0] if argv else ""
                _rec = self._qt_recording
                if _rec is None or (_rec.get("doc_id") or "") != _doc_name:
                    return _Outcome(0, QUICKTIME_DOC_GONE_MARKER + "\n", "")
                return _Outcome(0, "~/Movies/Screen Recording %s.mov\n" % _doc_name, "")
            if script == QUICKTIME_PLAY:
                return _Outcome(0, "0\n", "")
            # Probe scripts (and anything else): trivial success.
            return _Outcome(0, "0", "")