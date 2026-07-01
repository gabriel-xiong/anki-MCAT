// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use crate::collection::Collection;
use crate::error;

impl crate::services::McatService for Collection {
    fn get_topic_mastery(&mut self) -> error::Result<anki_proto::mcat::TopicMasteryList> {
        Ok(anki_proto::mcat::TopicMasteryList {
            entries: self.topic_mastery_all()?,
        })
    }

    fn get_topic_mastery_one(
        &mut self,
        input: anki_proto::mcat::TopicMasteryRequest,
    ) -> error::Result<anki_proto::mcat::TopicMastery> {
        self.topic_mastery_one(&input.topic_id)
    }
}
