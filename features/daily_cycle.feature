Feature: Daily evidence cycle
  The owner publishes a post, the crawler finds a mention, the archive poll confirms the
  timestamp, and the digest reports facts only. The evidence chain stays valid throughout.

  Scenario: One full day with a post, a mention and a failed source
    Given an empty brand-evidence database
    When a Threads post about Northwind is published and hooked
    And the daily crawl runs with one working source and one broken source
    And the archive poll runs and the archive confirms the post snapshot
    And the daily digest is built
    Then the digest lists the post with archive status "confirmed"
    And the digest lists 1 new mention
    And the digest names the failed source "broken"
    And the crawl run is recorded as "partial"
    And the evidence chain verifies with 13 entries

  Scenario: A quiet day produces an empty digest
    Given an empty brand-evidence database
    When the daily crawl runs with sources that find nothing
    And the daily digest is built
    Then the digest states "0 new mentions."
    And the digest states "0 posts captured."
    And the crawl run is recorded as "ok"
