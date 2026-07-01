// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::collections::HashMap;

use fsrs::FSRS;
use fsrs::FSRS5_DEFAULT_DECAY;

use crate::prelude::*;
use crate::scheduler::timing::SchedTimingToday;
use crate::tags::split_tags;

/// Note tags use `topic:{topic_id}` — see MCAT/data/deck-tagging.md.
pub const TOPIC_TAG_PREFIX: &str = "topic:";

/// Mirrors MCAT/data/scoring-config.json performance_eligibility.
pub const MIN_CARDS_SEEN_FOR_PERFORMANCE: u32 = 3;
pub const MIN_GOOD_OR_EASY_FOR_PERFORMANCE: u32 = 5;

#[derive(Debug, Clone, PartialEq)]
pub struct TopicStats {
    pub cards_total: u32,
    pub cards_seen: u32,
    pub good_or_easy_count: u32,
    pub retrievability_sum: f64,
    pub retrievability_count: u32,
}

impl TopicStats {
    fn avg_retrievability(&self) -> f32 {
        if self.retrievability_count == 0 {
            0.0
        } else {
            (self.retrievability_sum / self.retrievability_count as f64) as f32
        }
    }

    fn performance_unlocked(&self) -> bool {
        self.cards_seen >= MIN_CARDS_SEEN_FOR_PERFORMANCE
            && self.good_or_easy_count >= MIN_GOOD_OR_EASY_FOR_PERFORMANCE
    }
}

impl Collection {
    pub fn topic_mastery_all(&mut self) -> Result<Vec<anki_proto::mcat::TopicMastery>> {
        let stats = self.topic_stats_by_id()?;
        let mut entries: Vec<_> = stats
            .into_iter()
            .map(|(topic_id, s)| into_proto(topic_id, s))
            .collect();
        entries.sort_by(|a, b| a.topic_id.cmp(&b.topic_id));
        Ok(entries)
    }

    pub fn topic_mastery_one(&mut self, topic_id: &str) -> Result<anki_proto::mcat::TopicMastery> {
        let stats = self.topic_stats_by_id()?;
        stats
            .get(topic_id)
            .cloned()
            .map(|s| into_proto(topic_id.to_string(), s))
            .or_not_found(topic_id)
    }

    fn topic_stats_by_id(&mut self) -> Result<HashMap<String, TopicStats>> {
        let timing = self.timing_today()?;
        let mut topic_notes: HashMap<String, Vec<NoteId>> = HashMap::new();

        let note_tags_list = self
            .storage
            .get_note_tags_by_predicate(|tags| tags.contains(TOPIC_TAG_PREFIX))?;
        for note_tags in note_tags_list {
            for tag in split_tags(&note_tags.tags) {
                if let Some(topic_id) = tag.strip_prefix(TOPIC_TAG_PREFIX) {
                    if topic_id.is_empty() {
                        continue;
                    }
                    topic_notes
                        .entry(topic_id.to_string())
                        .or_default()
                        .push(note_tags.id);
                }
            }
        }

        let mut out = HashMap::new();
        for (topic_id, note_ids) in topic_notes {
            let mut stats = TopicStats {
                cards_total: 0,
                cards_seen: 0,
                good_or_easy_count: 0,
                retrievability_sum: 0.0,
                retrievability_count: 0,
            };

            for nid in note_ids {
                for card in self.storage.all_cards_of_note(nid)? {
                    stats.cards_total += 1;
                    let revlog = self.storage.get_revlog_entries_for_card(card.id)?;
                    let mut seen = false;
                    for entry in &revlog {
                        if !entry.has_rating() {
                            continue;
                        }
                        if !seen {
                            stats.cards_seen += 1;
                            seen = true;
                        }
                        if entry.button_chosen >= 3 {
                            stats.good_or_easy_count += 1;
                        }
                    }
                    if let Some(r) = self.card_retrievability(&card, &timing)? {
                        stats.retrievability_sum += r as f64;
                        stats.retrievability_count += 1;
                    }
                }
            }
            out.insert(topic_id, stats);
        }
        Ok(out)
    }

    fn card_retrievability(
        &self,
        card: &Card,
        timing: &SchedTimingToday,
    ) -> Result<Option<f32>> {
        let Some(state) = card.memory_state else {
            return Ok(None);
        };
        let last_review = if let Some(t) = card.last_review_time {
            t
        } else {
            match self.storage.time_of_last_review(card.id)? {
                Some(t) => t,
                None => return Ok(None),
            }
        };
        let seconds = timing.now.elapsed_secs_since(last_review) as u32;
        let decay = card.decay.unwrap_or(FSRS5_DEFAULT_DECAY);
        Ok(Some(
            FSRS::new(None)
                .unwrap()
                .current_retrievability_seconds(state.into(), seconds, decay),
        ))
    }
}

fn into_proto(topic_id: String, stats: TopicStats) -> anki_proto::mcat::TopicMastery {
    anki_proto::mcat::TopicMastery {
        topic_id,
        cards_total: stats.cards_total,
        cards_seen: stats.cards_seen,
        good_or_easy_count: stats.good_or_easy_count,
        avg_retrievability: stats.avg_retrievability(),
        performance_unlocked: stats.performance_unlocked(),
    }
}

#[cfg(test)]
mod test {
    use super::*;
    use crate::tests::NoteAdder;

    fn add_topic_note(col: &mut Collection, topic_id: &str) -> Note {
        let mut note = NoteAdder::basic(col).note();
        note.tags = vec![format!("{TOPIC_TAG_PREFIX}{topic_id}")];
        col.add_note(&mut note, DeckId(1)).unwrap();
        note
    }

    #[test]
    fn empty_collection_has_no_topics() -> Result<()> {
        let mut col = Collection::new();
        assert!(col.topic_mastery_all()?.is_empty());
        Ok(())
    }

    #[test]
    fn counts_seen_and_good_reviews() -> Result<()> {
        let mut col = Collection::new();
        add_topic_note(&mut col, "bb_enzymes");
        let mut note = NoteAdder::basic(&mut col).note();
        note.tags = vec!["topic:bb_enzymes".into()];
        col.add_note(&mut note, DeckId(1)).unwrap();

        col.answer_good();
        col.clear_study_queues();
        col.answer_good();
        col.clear_study_queues();

        let mastery = col.topic_mastery_one("bb_enzymes")?;
        assert_eq!(mastery.cards_total, 2);
        assert_eq!(mastery.cards_seen, 2);
        assert_eq!(mastery.good_or_easy_count, 2);
        assert!(!mastery.performance_unlocked);

        Ok(())
    }

    #[test]
    fn performance_unlocked_at_thresholds() -> Result<()> {
        let mut col = Collection::new();
        for _ in 0..3 {
            add_topic_note(&mut col, "cp_acids_bases");
        }
        for _ in 0..5 {
            col.answer_good();
            col.clear_study_queues();
        }

        let mastery = col.topic_mastery_one("cp_acids_bases")?;
        assert_eq!(mastery.cards_seen, 3);
        assert_eq!(mastery.good_or_easy_count, 5);
        assert!(mastery.performance_unlocked);

        Ok(())
    }

    #[test]
    fn unknown_topic_is_not_found() {
        let mut col = Collection::new();
        assert!(col.topic_mastery_one("bb_glycolysis").is_err());
    }
}
