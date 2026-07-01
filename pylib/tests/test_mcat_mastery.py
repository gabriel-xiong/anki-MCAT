# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from tests.shared import getEmptyCol


def test_topic_mastery_unlock():
    col = getEmptyCol()
    assert col.get_topic_mastery() == []

    for i in range(3):
        note = col.newNote()
        note["Front"] = f"card {i}"
        note.tags = [f"topic:cp_acids_bases"]
        col.addNote(note)

    for _ in range(5):
        c = col.sched.getCard()
        assert c is not None
        col.sched.answerCard(c, 3)

    mastery = col.get_topic_mastery_one("cp_acids_bases")
    assert mastery.cards_total == 3
    assert mastery.cards_seen == 3
    assert mastery.good_or_easy_count == 5
    assert mastery.performance_unlocked is True

    entries = col.get_topic_mastery()
    assert len(entries) == 1
    assert entries[0].topic_id == "cp_acids_bases"
