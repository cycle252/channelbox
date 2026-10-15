# ChannelBox development

Read README.md and docs/INSTALL.md before changing startup or platform behavior.
Keep user data, stream addresses, tokens and credentials in ignored private/.
Preserve channel numbering, favorites, previous-channel behavior and state.
Do not replace tvg-id with player identities; schedules use the original ID.
Do not publish a real playlist, EPG database, SSH key, provider or Drive URL.
Use examples with example.invalid and synthetic test data.
Run python3 -m unittest discover -s tests -v, the example playlist listing,
node --check web/app.js, and git diff --check after relevant code changes.
Distinguish automated checks from real hardware validation.
Do not enable startup or overwrite an existing installation without user intent.
