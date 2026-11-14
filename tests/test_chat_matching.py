from streakbot.tiktok.client import Profile, match_chat_rows

STRANGER_SAM = (Profile("sam", "sam", "u1"), "avt/default")
FRIEND_SAM = (Profile("sam.r_07", "sam", "u2"), "avt/tshy123")
L1 = (Profile("jordan_9", "ᴊᴏʀᴅᴀɴ", "u3"), "avt/l1en")
SELF = (Profile("owner.acct", "owner", "u4"), "avt/me")


def test_avatar_uri_disambiguates_same_display_name():
    rows = [{"nick": "sam", "src": "https://cdn/x/avt/tshy123~c5.jpeg"}]
    assert [p.username for p in match_chat_rows(rows, [STRANGER_SAM, FRIEND_SAM, SELF], 20)] == ["sam.r_07"]


def test_name_fallback_returns_all_colliding_accounts():
    rows = [{"nick": "sam", "src": ""}]
    assert [p.username for p in match_chat_rows(rows, [STRANGER_SAM, FRIEND_SAM], 20)] == ["sam", "sam.r_07"]


def test_order_follows_inbox_and_dedups_and_skips_unmatched():
    rows = [
        {"nick": "ᴊᴏʀᴅᴀɴ", "src": ""},
        {"nick": "group chat", "src": "multi"},
        {"nick": "sam", "src": "avt/tshy123"},
        {"nick": "ᴊᴏʀᴅᴀɴ", "src": "avt/l1en"},
    ]
    got = match_chat_rows(rows, [STRANGER_SAM, FRIEND_SAM, L1, SELF], 20)
    assert [p.username for p in got] == ["jordan_9", "sam.r_07"]


def test_limit():
    rows = [{"nick": "sam", "src": ""}, {"nick": "ᴊᴏʀᴅᴀɴ", "src": ""}]
    assert len(match_chat_rows(rows, [STRANGER_SAM, FRIEND_SAM, L1], 2)) == 2
