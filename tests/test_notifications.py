"""
tests/test_notifications.py — Mixtape

Regression test for Issue 4: rating a song should notify its sharer,
the same way adding it to a playlist does.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_song(app):
    """A song shared by one user, with a second user available to act on it."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="The Wanderers", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, seed_song):
    """
    Rating a friend's song should create a notification for the sharer,
    mirroring the existing behavior for adding a song to a playlist.
    """
    with app.app_context():
        sharer = seed_song["sharer"]
        rater = seed_song["rater"]
        song = seed_song["song"]

        before = get_notifications(sharer.id)
        rate_song(rater.id, song.id, 5)
        after = get_notifications(sharer.id)

        assert len(after) == len(before) + 1
        assert after[0]["type"] == "song_rated"


def test_rating_your_own_song_does_not_self_notify(app, seed_song):
    """Rating a song you shared yourself should not generate a notification."""
    with app.app_context():
        sharer = seed_song["sharer"]
        song = seed_song["song"]

        before = get_notifications(sharer.id)
        rate_song(sharer.id, song.id, 4)
        after = get_notifications(sharer.id)

        assert len(after) == len(before)
