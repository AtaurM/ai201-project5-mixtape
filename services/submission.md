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
