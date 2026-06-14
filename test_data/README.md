# test_data/

Mock fixtures for development and tests.

## Files

- `tcgplayer_fixture.csv` — synthetic TCGPlayer PRO Seller export. Hand-written, not real data. Used by `FixtureTCGPlayerSource` and the Phase 1 tests.
- `tcgplayer_fixture_v2.csv` — same as v1 but with quantity changes and one new card, to exercise the diff engine in tests.

## Why both `.gitignore` and committed files?

The folder's `.gitignore` excludes everything by default so real PRO Seller exports (which contain proprietary store data) never land in the repo. The synthetic fixtures are explicitly allow-listed so they DO commit.

If you drop a real export here while testing, name it anything other than `tcgplayer_fixture*.csv` and it'll stay untracked.
