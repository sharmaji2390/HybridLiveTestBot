HYBRID LIVE TEST BOT
====================

This project has TWO processes:

1) live_test_bot.py
   REAL Telegram Bot account.
   - Sends live quiz polls
   - Deletes each question after timer
   - Immediately sends next question
   - Receives poll votes
   - Calculates results
   - Generates final result + answer-key PDF
   - Uses -0.33 negative marking

2) source_reader_userbot.py
   Telegram USER SESSION used ONLY as a source reader.
   - Reads polls from chats/channels the user account can access
   - Imports Quiz + Regular polls
   - Imports anonymous + closed polls
   - Sends a JSON package to the real bot
   - Does NOT run the live test

IMPORTANT HOSTING
=================
The source_reader_userbot.py is a user-session/selfbot component.
If a hosting provider prohibits user sessions/selfbots, DO NOT run that
component there. Run it on a provider/environment that explicitly allows it.
The live_test_bot.py can run separately on a normal bot-friendly host.

SETUP
=====
A) LIVE BOT HOST
----------------
Environment:
BOT_TOKEN=...
API_ID=...
API_HASH=...
TEST_PLAY_ALLOWED_USER_IDS=6345786041,7224213357
SOURCE_READER_USER_IDS=6345786041
HYBRID_MODE=1

Upload:
live_test_bot.py
requirements.txt

Set startup file to:
live_test_bot.py

Start the bot once and send /start to it from the source-reader account.
This creates the Bot API private chat permission needed for the source reader
to send the import JSON package.

B) SOURCE READER HOST
---------------------
Environment:
API_ID=...
API_HASH=...
SOURCE_BOT_USERNAME=@YourLiveTestBot
SOURCE_ALLOWED_USER_IDS=6345786041
SOURCE_SESSION_NAME=live_test_source_reader

Upload:
source_reader_userbot.py
requirements.txt

Startup file:
source_reader_userbot.py

First start will require Telegram user-session login/verification.
After the session is created, the source reader can be restarted normally.

USE
===
In the TARGET GROUP, where both the live bot and source reader have access:

/import https://t.me/SomeChannel/12345 20 30

or:

/import https://t.me/c/1234567890/12345 20 auto

The source reader reads forward from that message ID until it finds the
requested number of polls, then sends the import package to the live bot.

After the bot confirms the test ID:

/test

or:

/test <test_id>

During the live test:
Question -> timer -> delete -> next question immediately.
Result calculation remains in memory/database and the final PDF is generated
after the last question.

POLL ANSWERS
============
Quiz polls have a Telegram-provided correct option and are scored normally.

Regular polls do NOT have a Telegram correct answer. They are imported with
correct=-1 and appear as UNKEYED, so the bot cannot truthfully award +1/-0.33
unless a correct answer is supplied separately.

Anonymous/closed status does not itself prevent importing the poll's
question/options. Historical voter identity is not imported.

SECURITY
========
Never put BOT_TOKEN/API_HASH into public code or send them in chat.
Do not reuse the same user session on a host that bans selfbots.
