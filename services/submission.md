# Mixtape Codebase Map

## Overview

Mixtape is a small Flask app for sharing songs with friends, rating them, building collaborative playlists, and tracking listening streaks. It's organized in three layers: `routes/` (HTTP), `services/` (business logic), and `models.py` (data shape). No FE; just JSON returned by each route.

## Main files

**app.py** holds the application builder, `create_app()`. It builds the Flask app, configures a SQLite database (overridable via a `DATABASE_URL` env var), initializes the shared `db = SQLAlchemy()` instance, and registers the four blueprints under `/songs`, `/playlists`, `/users`, and `/feed`. It also calls `db.create_all()` on startup, so there's no separate migration step for local dev.

**models.py** defines the schema: `User`, `Tag`, `Song`, `ListeningEvent`, `Rating`, `Playlist`, and `Notification`, plus three raw association tables. `friendships` is a symmetric many-to-many self-join on `User` (friendship rows get inserted twice, once per direction, as seen in `seed_data.py`). `song_tags` is a plain many-to-many between songs and tags. `playlist_entries` is the interesting one: it's not a bare join table, it carries `position`, `added_by`, and `added_at` columns, which is how a playlist keeps explicit song ordering instead of relying on insertion order. Every model has a `to_dict()` used directly as the JSON response body, so there's no separate serializer layer.

**routes/songs.py, playlists.py, users.py, feed.py** are Flask blueprints. Each route function does argument extraction from the request, a call into exactly one service function, and either a JSON success response or a `400`/`404` built from a caught `ValueError`. None of them touch `db` or query models directly except `users.py`, which does a direct `db.session.get(User, ...)` for the plain user-lookup endpoint since there's no service function dedicated to that.

**services/notification_service.py** owns notification creation and retrieval (`create_notification`, `get_notifications`, `mark_as_read`), but it also owns two actions that aren't really about notifications on their own: `add_to_playlist` and `rate_song`. Both live here because both are the triggers for a notification, so the module is really "things that cause or manage notifications" rather than strictly CRUD on the `Notification` table.

**services/playlist_service.py** covers playlist creation and reads: `create_playlist`, `get_playlist`, `get_playlist_songs`, `get_user_playlists`. Song ordering within a playlist is read back out via a join against `playlist_entries` sorted by `position`.

**services/search_service.py** is a thin case-insensitive `ILIKE` search over `Song.title` and `Song.artist`, plus a single-song lookup by ID.

**services/streak_service.py** contains the day-based streak counter: `record_listening_event` writes a `ListeningEvent` row and then calls `update_listening_streak`, which compares the calendar date of the new event against `User.last_listened_at` to decide whether to hold, increment, or reset the streak to 1.

**services/feed_service.py** builds the two friend-facing feeds: `get_friends_listening_now` (recent listening events within a 24-hour window, de- duplicated to one entry per friend) and `get_activity_feed` (the most recent N listening events across all friends, no time cutoff).

**seed_data.py** is a standalone script (`python seed_data.py`) that drops and recreates all tables, then populates five users with friendships, songs with varying tag counts, playlists with ordered entries, a mix of recent and old listening events, and a couple of pre-existing notifications. It's meant to be re-run freely since it starts with `db.drop_all()`.

**tests/** has three files, `test_playlists.py`, `test_search.py`, `test_streaks.py`, each exercising one service module against a fresh app/db fixture.

## Data flow: adding a song to a playlist triggers a notification

1. Client calls `POST /playlists/<playlist_id>/songs` with `song_id` and `added_by` in the body.
2. `routes/playlists.py:add_song` pulls those two fields out of the request and, if both are present, calls `add_to_playlist(playlist_id, song_id, added_by)` from `services/notification_service.py`.
3. Inside `add_to_playlist`, the function loads the `Song`, the adding `User`, and the `Playlist` by ID, raising `ValueError` (which the route turns into a 400) if any is missing.
4. If the song isn't already in `playlist.songs`, it's appended and the change is committed. This append is what writes a row into the `playlist_entries` join table.
5. Then, only if `song.shared_by != added_by_user_id` (i.e., you didn't add your own shared song to a playlist), it calls `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", body=...)`.
6. `create_notification` builds a `Notification` row addressed to the original sharer and commits it.
7. Later, the sharer fetches `GET /users/<user_id>/notifications`, which calls `get_notifications` in the same service, ordered newest-first, optionally filtered to unread only. A separate `POST /users/notifications/<id>/read` flips the `read` flag via `mark_as_read`.

Note that the same "act on a song, notify the sharer" shape shows up for ratings too: `rate_song` also lives in `notification_service.py` even though today it only writes the `Rating` row and doesn't call `create_notification` itself. The module groups by "who's affected," not by which table gets written.

## Patterns

- **Routes are minimal, services hold logic.** Every blueprint handler parses input and formats output; the actual work (lookups, validation, writes, cross-entity effects) is in `services/`. This makes the services independently testable, which is exactly what `tests/` does.
- **Validation via exceptions, not return codes.** Services raise `ValueError` with a human-readable message on any bad ID or invalid input; routes catch it and turn it into a 400 or 404 depending on context. There's no shared exception type per error class, just the message string.
- **Services are grouped by what is relevant, not by table.** `notification_service.py` is the clearest example: it holds `rate_song` and `add_to_playlist`, not just notification CRUD, because both are the events that can produce a notification. `models.py`, by contrast, is grouped strictly by table.
- **Serialization lives on the model.** `to_dict()` methods on each model are the only serialization path; services and routes just call them rather than building response dicts by hand.
- **IDs are UUID strings everywhere**, generated in Python via `generate_uuid()` rather than relying on autoincrementing integer primary keys.
- **Association tables carry metadata when the relationship needs it.** `playlist_entries` has `position`/`added_by`/`added_at`; `friendships` and `song_tags` are bare join tables since there's nothing extra to track for those relationships.

## Bug reproduction notes (before any fixes)

Setup:

```bash
python seed_data.py
FLASK_APP=app:create_app flask run
```

Then look up real IDs to plug into the requests below, since every endpoint needs a UUID:

```bash
curl http://127.0.0.1:5000/songs/search?q=a
```

The response includes `id` and `shared_by` for every song. Cross-reference `shared_by` against `GET /users/<id>` if you need a username. From the seeded data, I used `nova` as a song sharer and `darius` as a second user, and one of nova's playlists.

**Issue 4, notified on playlist add but not on rating.** Checked the sharer's notification count, rated one of their songs as a different user, then checked again.

```bash
curl http://127.0.0.1:5000/users/<nova_id>/notifications
# {"count":1, ...}

curl -X POST http://127.0.0.1:5000/songs/<song_id>/rate \
  -H "Content-Type: application/json" \
  -d '{"user_id": "<darius_id>", "score": 5}'
# {"id":"...", "score":5, ...}  <- the rating was created

curl http://127.0.0.1:5000/users/<nova_id>/notifications
# {"count":1, ...}  <- unchanged, no new notification for the sharer
```

The rating call succeeds and returns a real rating record, but the sharer's notification count doesn't move. Compare this against adding a song to a playlist (`POST /playlists/<id>/songs`), which does bump the sharer's count by one, to see the asymmetry directly.

**Issue 5, last playlist song never shows up.** Compared the playlist's own metadata against what the songs endpoint returns.

```bash
curl http://127.0.0.1:5000/playlists/<playlist_id>
# {"name": "Late Night Vibes", ...}

curl http://127.0.0.1:5000/playlists/<playlist_id>/songs
# {"count": 6, "songs": [...]}
```

Seed data puts 7 songs in this playlist (confirmed by counting the insert statements in `seed_data.py` for that playlist), but the endpoint reports 6. Adding one more song with `POST /playlists/<playlist_id>/songs` and re-checking the count shows it still comes up one short of what was actually added.

**Issue 2, feed shows people from yesterday.** The `/songs/<id>/listen` route always stamps the event with the server's current time, there's no way to submit a past timestamp through the API. So a live repro needs one setup step outside the API: pick a friend, keep exactly one of their listening events, and set that event's `listened_at` back by 20 hours (done with a short script against the same database, not through any route, since the app itself has no "backdate" feature). That step simulates a real friend whose last listen genuinely happened 20 hours ago:

```bash
curl http://127.0.0.1:5000/feed/<nova_id>/listening-now
```

With darius's only remaining event 20 hours old, dated the previous calendar day relative to "now," he still showed up in the feed response, tagged with yesterday's date in `listened_at`. The 24-hour rolling window doesn't care that the event crossed midnight, it only checks elapsed hours.

**Issue 1, streak resets.** Same limitation as Issue 2: `/songs/<id>/listen` always uses the real current server time, and the buggy comparison only misfires when the request actually lands on a Sunday. There's no way to fake "today is Sunday" through the API without changing the system clock, which I didn't want to do since it affects the whole machine, not just this app. The reproducible curl procedure is:

```bash
# On a Saturday:
curl -X POST http://127.0.0.1:5000/songs/<song_id>/listen \
  -H "Content-Type: application/json" -d '{"user_id": "<user_id>"}'

# On the following Sunday:
curl -X POST http://127.0.0.1:5000/songs/<song_id>/listen \
  -H "Content-Type: application/json" -d '{"user_id": "<user_id>"}'

curl http://127.0.0.1:5000/users/<user_id>/streak
```

Run for real on those two days, the streak should read 2 and instead reads 1. Since I can't wait for a real Saturday-to-Sunday transition to write this up, I cross-checked the same logic by calling `update_listening_streak` directly with constructed dates spanning a Saturday into a Sunday: streak climbed 1 through 6 across a full week, Monday through Saturday, then dropped back to 1 on Sunday instead of continuing to 7, confirming the same code path fails specifically on that boundary regardless of how the date is supplied.

**Issue 3, duplicate search results.** Tried this one the same way as the others:

```bash
curl "http://127.0.0.1:5000/songs/search?q=Crown"
```

against a song seeded with three tags. It came back with `"count":1`, one result, not three. I checked whether the join underneath is actually fanning out by running the equivalent SQL directly against the same database outside the app: it returns 3 duplicate rows, one per tag, confirming the join itself is still wrong. But the ORM call search_service.py makes on top of that join (`session.query(Song)...all()`) is deduplicating full-entity rows by primary key before the route ever serializes a response, on the pinned SQLAlchemy version (2.0.51). `tests/test_search.py` also passes as-is, consistent with what curl showed. I'm flagging this one as not reproducible through the running app in this environment rather than counting it as one of the three to fix, unless further digging turns up a path that does surface the duplicates in the actual JSON response.

Based on this, issues 1, 2, 4, and 5 reproduce cleanly through the running server. Issue 3 does not reproduce in an actual HTTP response in this environment, so it needs more investigation before deciding whether to count it toward the fix total.

## Root cause analysis

### Issue 1: My listening streak keeps resetting

**How I reproduced it:** Called `update_listening_streak` with constructed dates for ten consecutive days starting on a Monday (see reproduction notes above; a live curl repro needs a real Saturday-to-Sunday pair of days since the `/listen` route always uses server "now"). The streak climbed 1 through 6 correctly Monday through Saturday, then dropped to 1 on Sunday instead of continuing to 7.

**How I found the root cause:** Opened `services/streak_service.py` and read `update_listening_streak` top to bottom. The three branches are: same day (no-op), one day since last listen (increment), anything else (reset to 1). The increment branch read `elif days_since_last == 1 and today.weekday() != 6`. That extra clause was the only thing distinguishing Sunday from every other day, and it directly matched the bug report's timing.

**The root cause:** Python's `date.weekday()` returns 6 for Sunday. The increment branch required `days_since_last == 1` *and* `today.weekday() != 6`, so a listen on Sunday, even one day after a Saturday listen, always failed the second condition and fell through to the `else` branch, which resets the streak to 1. The condition had nothing to do with whether the streak was actually consecutive, it just special-cased Sunday out of the one legitimate increment path.

**My fix and side-effect check:** Removed `and today.weekday() != 6`, leaving `elif days_since_last == 1:` as the sole condition for incrementing. Reran the same ten-day simulation; the streak now goes 1 through 10 without interruption across the Sunday boundary. Ran the full test suite afterward: `tests/test_streaks.py` passes, and the only failures are the two pre-existing `test_playlists.py` failures tied to Issue 5, unrelated to this change.
