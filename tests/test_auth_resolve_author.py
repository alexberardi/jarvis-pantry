"""Which Author a verified GitHub login maps to (app.auth.resolve_author).

The id is the identity; a username match may only claim a pre-OAuth row
(github_id == 0) and never a reserved service row, or anyone who registers
(or renames into) that username would inherit its packages.
"""

from app.auth import resolve_author
from app.models import Author


def _user(github_id: int, login: str) -> dict:
    return {"id": github_id, "login": login, "name": None, "avatar_url": None}


def _author(db, github_id: int, username: str) -> Author:
    a = Author(github_id=github_id, github_username=username, display_name=username)
    db.add(a)
    db.commit()
    return a


def test_known_id_returns_its_author(db_session):
    a = _author(db_session, 101, "alice")
    assert resolve_author(db_session, _user(101, "alice")).id == a.id


def test_new_user_gets_a_new_author(db_session):
    a = resolve_author(db_session, _user(102, "bob"))
    assert (a.github_id, a.github_username) == (102, "bob")


def test_pre_oauth_row_is_claimed_by_its_username(db_session):
    a = _author(db_session, 0, "carol")
    got = resolve_author(db_session, _user(103, "carol"))
    assert got.id == a.id and got.github_id == 103


def test_operator_row_is_never_claimed(db_session):
    operator = _author(db_session, 0, "pantry-operator")
    got = resolve_author(db_session, _user(201, "pantry-operator"))
    db_session.refresh(operator)
    assert got.id != operator.id
    assert operator.github_id == 0  # still the service row
    assert got.github_username == "pantry-operator~201"


def test_a_sentinel_id_row_is_never_claimed(db_session):
    local = _author(db_session, -1, "local")
    got = resolve_author(db_session, _user(202, "local"))
    assert got.id != local.id and got.github_username == "local~202"


def test_someone_elses_username_is_not_inherited(db_session):
    # dave renamed away on GitHub; a new account took the name "dave".
    dave = _author(db_session, 104, "dave")
    newcomer = resolve_author(db_session, _user(999, "dave"))
    assert newcomer.id != dave.id
    assert newcomer.github_id == 999
    assert newcomer.github_username == "dave~999"


def test_rename_into_a_taken_name_does_not_collide(db_session):
    _author(db_session, 105, "erin")
    frank = _author(db_session, 106, "frank")
    got = resolve_author(db_session, _user(106, "erin"))
    assert got.id == frank.id and got.github_username == "erin~106"


def test_rename_updates_the_username(db_session):
    g = _author(db_session, 107, "gina")
    got = resolve_author(db_session, _user(107, "gina-new"))
    assert got.id == g.id and got.github_username == "gina-new"
