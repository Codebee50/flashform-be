# Data & privacy

What Flashform stores about students and teachers, who can see it, and how to delete it.

[← Back to README](../README.md)

This page describes the code in this repository as it is today. Flashform is a prototype
and has not been independently security-reviewed.

## Summary

| What we store | What we don't store |
|---|---|
| Students: the display name they type, their answers, and when they joined, answered and finished | Student accounts, emails, passwords or real names (any name works) |
| Teachers: email address, display name, a hashed password, and whether the email is verified | Student IP addresses, devices or locations (in the database) |
| Teachers' rooms, quizzes and activity results | Cookies for students, analytics or tracking scripts |
| A scrambled (hashed) copy of each student's session token | Plain-text passwords or plain-text student tokens |

Data is kept until a teacher deletes it. There is no automatic deletion yet (see
[Not yet implemented](#not-yet-implemented)).

## Students

Students never create an account. To join, a student types a room code and a display
name. The name can be anything from 1 to 40 characters. It does not need to be a real
name.

Each time a student joins an activity (one quick question or one quiz run), Flashform
stores one participant record with:

- the display name;
- when they joined;
- when they were last connected (updated at most once every 30 seconds while their
  screen is open);
- when they pressed Finish, for self-paced quizzes;
- whether they left or were removed by the teacher;
- their personal question order, if the teacher shuffled the questions;
- a hash of their session token (see [Authentication](#authentication)).

For each question they answer, Flashform stores:

- the choice they picked, or the text they typed (up to 500 characters);
- whether the answer was correct, if the question has a correct answer;
- whether the answer is locked (can no longer be changed);
- when it was submitted.

Participant records for different activities are not linked to each other in the
database. A student who answers three activities in the same room appears as three
separate participants with the same name.

**What is not collected from students:**

- no email address, password, phone number or account;
- no IP address, device or location data in the database (IP addresses are used briefly
  for rate limiting; see [Logs and rate limits](#logs-and-rate-limits));
- no cookies are set by the API for students;
- no analytics, advertising or tracking code. None was found in the frontend either.

**On the student's own device:** the frontend saves the student's name, their session
token and any answer still waiting to be sent in the browser's local storage for that
room. This is how a student gets back to the same question after a page refresh. "Leave
room" clears it.

## Teachers

Teachers create an account with a name, an email address and a password. Flashform
stores:

- the email address (lowercased);
- the display name;
- the password, hashed (see below);
- whether the email address is verified, and when;
- when the account was created and when the teacher last logged in (standard Django user
  fields);
- a record of each login session's refresh token, so sessions can be logged out and
  revoked.

It also stores what the teacher creates: rooms (name, code, locked or not), quizzes
(title, questions, correct answers, explanations) and activities with their results.

**Passwords** are never stored in plain text. They are hashed with Django's default
hasher, PBKDF2 with SHA-256 and a random salt per password (1,000,000 iterations in the
installed Django 5.2). Passwords must be at least 8 characters, not a common password,
not all digits, and not too similar to the name or email.

**On the teacher's device:** the frontend keeps the teacher's login tokens in the
browser's local storage, so a page reload does not log them out. The "Hide names"
setting is also kept there.

## Authentication

**Teachers** log in with email and password and get two JSON Web Tokens (JWTs):

- an access token, valid for 30 minutes, sent with each request;
- a refresh token, valid for 14 days, used to get a new access token. Each refresh token
  works once. Using it returns a new one and revokes the old one.

A teacher must click the link in a verification email before they can log in. The link
is valid for 3 days. Logging out revokes the refresh token. A password reset link is
valid for 1 hour and works once. A successful reset logs the teacher out on every device.

**Students** get a random secret token when they join an activity. The server sends it
to the student's browser once and stores only its SHA-256 hash. The token only works for
that one activity. It stops working when the student leaves or the teacher removes them.

Login, registration, email and join requests are rate limited to slow down password
guessing and abuse.

The live-update connection (WebSocket) sends the token in its URL: the teacher's access
token as `?auth=` and the student's token as `?token=`. Server or proxy access logs can
record these URLs. A teacher's access token expires within 30 minutes. A student's token
lasts as long as the activity.

## Third parties

| Service | What it receives | When |
|---|---|---|
| [Brevo](https://www.brevo.com/) (email delivery) | The teacher's email address and display name, and the email itself, which contains the verification or password reset link | Only when a teacher registers, asks for a new verification email, or asks for a password reset. Only if a Brevo API key is configured |
| Hosting provider | Everything the server stores and processes | Always. The live instance at https://flashform.live runs its backend, database and Redis on [Railway](https://railway.com/). Its frontend is served by [Vercel](https://vercel.com/) |

No student data is sent to Brevo. No other outside service receives data from the
backend.

## Who can see what

**Teachers** see only their own rooms, quizzes, activities and reports. Every teacher
request is checked against the owner. Another teacher's room, quiz or report answers "not
found", as if it did not exist. The live-update connection for a room also checks that
the teacher owns it. For their own activities, a teacher sees each student's display
name, answers, score, join time and last-connected time.

**Students** see:

- the room's name, and whether it is locked or has an activity running (anyone with the
  room code can see this);
- the question or questions currently open to them;
- their own answers.

Students never see other students' names or answers, or the answer counts. They see a
correct answer only if the teacher turned on "Show correct answer" for the quiz, and only
after their own answer to that question is locked. Live-update messages to students
carry no answers or names, only "something changed, fetch again". The test suite checks
that nothing a student can fetch or receive reveals a correct answer or a classmate's
answer (`tests/test_security.py`).

**Whoever runs the server** can see all data. That includes anyone with access to the
database, and staff accounts on the Django admin site (`/admin/`), which lists users,
rooms, quizzes, activities, participants and answers. On the live instance, that is the
author.

## Deleting data

What exists today:

- **Delete a room:** deletes the room and all of its activities, participants and answers.
- **Delete a report:** deletes an ended activity with its questions, participants and
  answers.
- **Delete a quiz:** deletes the quiz and its questions. Reports of activities already run
  from it keep their own copy of the questions and are not deleted.
- **Remove a student** from an activity: their token stops working and they disappear
  from the live view, the report and the CSV. Their answers stay in the database until
  the activity or room is deleted.
- **A student presses "Leave room"** during an activity: same as being removed. Their
  answers are kept but left out of the report. Leaving after the activity has ended
  changes nothing, so they stay in that report.
- **Delete a teacher account:** there is no button for this yet. Someone who runs the
  server can delete the user in the Django admin. This deletes the teacher's rooms,
  quizzes, activities and all student answers in them. Records of the teacher's login
  tokens are kept, with the link to the account removed. They contain the account's
  numeric id, not the email. Expired ones can be removed with
  `python manage.py flushexpiredtokens`.

Deleted data is removed from the database right away. Database backups made by the host
are outside Flashform's control.

## Logs and rate limits

- **Rate limits** count requests per IP address (and, for email requests, per hashed email
  address) in Redis. The counters expire on their own after the rate-limit window, between
  1 minute and 1 hour.
- **Server logs** print one line per request, and the hosting provider may keep its own
  logs. These can include IP addresses and request URLs. How long they are kept depends on
  the host.
- **Email logs:** if an email fails to send, the teacher's email address and the email
  subject are written to the server log. With no Brevo API key configured (local
  development), the whole email, including its link, is written to the log instead of
  being sent.

## Self-hosting

A college can run its own copy of Flashform. Then all data stays in its own database and
Redis, on servers it controls. The code is open source (MIT) and runs with Docker. See
[development.md](development.md).

The only outside service is Brevo, for teacher emails. It is optional: without a Brevo
API key, emails are written to the server log instead of sent. The server operator can
then verify teachers with `python manage.py verify_teacher <email>`. Password reset emails
will not arrive in that setup.

## Not yet implemented

Things a reviewer might expect that do not exist yet:

- **Automatic data retention limits.** Student answers and names are kept until a teacher
  deletes the activity or room.
- **Self-service account deletion or data export** for teachers.
- **Removing a single student's answers** from the database. Removing a student hides them
  but keeps their answers.
- **A way for a student to delete their own answers.**
- **Sending account emails through the college's own mail server.** Verification and
  reset emails go through Brevo only.
- **Automatic cleanup** of expired login-token records.
- **An audit log** of who viewed or exported data.
- **Encryption of stored data by Flashform itself.** Encryption at rest depends on the
  database host.
- **An independent security review.**
