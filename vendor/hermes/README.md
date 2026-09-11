# Hermes-derived document conversion

`xlsx_to_blocks.py` is the spreadsheet renderer imported from the internal
Hermes reference snapshot. TYBot owns and executes this copy as part of its
ingestion pipeline; the Hermes specialist only searches and answers from the
already converted archive.

The renderer is kept outside `subbots/hermes` deliberately. Specialist releases
must not download Slack files, decide access, or write archive source material.

Other Hermes document formats used an external `kordoc` CLI and LibreOffice,
not repository source code. TYBot calls only preinstalled binaries through
`src/tybot/archive/external_convert.py`; it never performs an `npx -y` download
while handling a Slack message.
