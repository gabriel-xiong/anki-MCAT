// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! MCAT Speedrun extensions (read-only mastery query, performance tables later).

mod service;
mod topic_mastery;

pub use topic_mastery::MIN_CARDS_SEEN_FOR_PERFORMANCE;
pub use topic_mastery::MIN_GOOD_OR_EASY_FOR_PERFORMANCE;
pub use topic_mastery::TOPIC_TAG_PREFIX;
