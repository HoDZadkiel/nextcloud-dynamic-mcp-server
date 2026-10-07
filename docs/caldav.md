# CalDAV operations

Eight `caldav_*` operations close the same gap for calendars that WebDAV closes for files. Nextcloud's calendar app is CalDAV ([RFC 4791](https://www.rfc-editor.org/rfc/rfc4791)) end to end - there is no OCS REST API for it, so `ocs_api_viewer` has nothing to discover here. `dav_upcoming_events_get_events`, the one calendar-adjacent operation that *is* discovered, only powers the dashboard widget's next few events and offers no create/update/delete path.

They cover calendars (`caldav_list_calendars`, `caldav_create_calendar`, `caldav_delete_calendar`) and events (`caldav_list_events`, `caldav_get_event`, `caldav_create_event`, `caldav_update_event`, `caldav_delete_event`). Only `VEVENT` (ordinary calendar events) is handled - `VTODO` and `VJOURNAL` entries are neither returned nor created.

Event identity follows the same rule [sabre/dav](https://sabre.io/dav/building-a-caldav-client/) - the CalDAV library Nextcloud's server is built on - documents for its own clients: a calendar object's filename is not guaranteed to match its `UID`. Every operation that addresses one event therefore takes the `path` a prior `caldav_list_events`, `caldav_get_event` or `caldav_create_event` already returned, never one composed by hand - the same pattern `webdav_*` uses for file paths.

`caldav_update_event` is a full replace, like `webdav_write_file`: read the event with `caldav_get_event` first, apply the change to what it returned, and send everything back - fields left out are cleared, not preserved. Pass the `etag` back as `if_match` and a write computed from a stale read fails with 412 instead of discarding a concurrent edit, the same guard `webdav_write_file` uses.

`caldav_list_events` defaults to a 90-day window from now (`time_min`/`time_max` override it, `all_time` lifts it) because a CalDAV `REPORT` has no pagination - an unbounded query against a calendar with years of recurring events could return all of it. A recurring event is returned once, as its master; `override_count` says how many dated exceptions exist without expanding them, and `rrule` is passed through as a raw RFC 5545 value rather than translated from natural language.

Times need a UTC offset or `Z` - `2026-09-20T14:00:00+08:00`, not a bare local datetime - because CalDAV has no notion of which timezone the caller means. Reading back a time the server tagged with an IANA zone (`TZID=Asia/Taipei`) returns the wall-clock value plus that zone name rather than converting it to an absolute instant; this server does not parse `VTIMEZONE` tables, so treat a returned event's `timezone` field as informational.

**Verified against a live instance (2026-10-07).** All eight operations were run against a real Nextcloud using throwaway calendars: calendar create/list/delete (including the `confirm` guard and the `color` property, which Nextcloud accepted), event create/list/get/update/delete, `if_match` returning 412 on a stale ETag for both update and delete, duplicate `uid` rejection, a recurring event matched correctly inside and outside its `COUNT` window, and listing across every calendar at once. Calendar deletion is still treated as permanent because Nextcloud's calendar trash retention was not checked.

That run turned up four defects, since fixed, covered by tests in `test_main.py`, and re-verified against the redeployed server on the same day:
- An attendee or organizer `name` containing a comma was returned with a stray backslash (`A\, Alice`). `CN` is a parameter, not TEXT, so it is now double-quoted instead of backslash-escaped, and the parser no longer splits on a `:` or `;` inside quotes.
- `caldav_update_event` accepted a `uid` that differed from the stored one. It now reads the event first and refuses a mismatch (one extra GET per update).
- `end` earlier than `start` was accepted; it is now rejected. An `end` equal to `start` is still allowed.
- An invalid `rrule` came back as a raw 500 XML body from Nextcloud; it is now checked for `FREQ=` plus `NAME=value` parts before anything is sent.

A fifth, cosmetic one is fixed too: updating rewrote the whole object, so `CREATED` became the update time. The original `CREATED` is now carried over from the read that already checks the UID (an event with no `CREATED` is stamped with the update time). This was re-verified live as well: after an update `CREATED` was unchanged while `LAST-MODIFIED` moved forward.

Not tested: writing to a read-only shared calendar, and recurring events with dated exceptions (`override_count` > 0).
