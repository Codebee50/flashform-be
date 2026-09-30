# Features

What teachers and students can do in Flashform today.

[← Back to README](../README.md)

Everything on this page is built. The backend test suite covers the server side of each
feature. A few items (the QR code, "Hide names", automatic retries and the polling
fallback) live only in the [frontend](https://github.com/Codebee50/flashform-fe). The
list of things that are not built yet is at the end.

## For teachers

### Account

- Register with a name, email address and password.
- Verify the email address with the link Flashform sends (valid for 3 days). Login is
  refused until the email is verified. The login page can send a new link.
- Log in and log out. The teacher stays logged in across page reloads. A login ends
  after 14 days without use.
- Reset a forgotten password with an emailed link (valid for 1 hour, works once). This
  logs the teacher out on every device. The "forgot password" page never says whether an
  email address has an account.

### Rooms

A room is a permanent space that students join with its code.

- Create a room with a name. Flashform picks a unique 6-character code. The code never
  uses characters that are easy to confuse (`0 O 1 I L`).
- Or choose your own code: 4 to 10 letters and digits, unique across all rooms.
- See all your rooms, with their codes and whether an activity is running.
- Rename a room.
- Lock a room so new students cannot join. Students who already joined can keep
  answering, and can rejoin the next activity in that room. Unlock it again at any time.
- Delete a room, with all its activities and results.
- Show students a join link and a QR code for the room.

### Quizzes

A quiz is a saved, reusable list of questions.

- Create a quiz with a title and 1 to 100 questions.
- Question types:
  - **Multiple choice:** 2 to 6 options. Optionally mark one as correct.
  - **True/false:** optionally mark True or False as correct.
  - **Short answer:** optionally list up to 20 accepted answers. Matching ignores
    upper/lower case and extra spaces at the ends. There is no fuzzy matching.
- Each question has a prompt (up to 1,000 characters) and an optional explanation (up to
  2,000 characters) shown to students with feedback.
- Add, edit, delete and move questions up or down, then save the whole quiz at once.
- Duplicate a quiz (the copy is titled "<title> (copy)").
- Delete a quiz. Reports of activities already run from it are kept, with the questions
  as they were at the time.

### Quick Question

A single question with no preparation, often asked out loud.

- Start a multiple choice (options A to D by default, or 2 to 6 custom labels),
  true/false or short answer question.
- The question text is optional. Without it, students are asked to answer the question the
  teacher asked.
- Quick questions have no correct answer, so they are not scored.
- **Start Vote:** for a short answer question, turn the students' distinct answers into
  the options of a new multiple choice question. Answers that differ only by case or
  surrounding spaces count as one. At least 2 distinct answers are needed.

### Running a quiz

- Start a saved quiz in a room in one of two modes:
  - **Teacher-paced:** everyone sees the same question. The teacher moves with Next and
    Previous. Moving on locks the answers to the question being left. A student who had
    not answered it can still do so if the teacher goes back to it.
  - **Student-paced:** each student sees every question, moves through them at their own
    speed, can change answers, and presses Finish at the end. Finish locks their answers.
- Option **Show correct answer after each response**: each answer locks as soon as it is
  submitted, and the student then sees whether it was right, the correct answer and the
  explanation.
- Option **Shuffle question order** (student-paced only): each student gets their own
  random order, kept for the whole activity.
- The quiz's questions are copied when the activity starts. Editing the quiz later does
  not change a running activity or its report.
- A room runs one activity at a time. Starting a new one ends the current one.
- End the activity at any time. All answers are then locked.

### Live results

While an activity runs, the teacher's screen updates within about a second of each
answer.

- Number of students joined, and how many answered each question.
- Answer counts per option. For short answer questions, answers are grouped (ignoring
  case and surrounding spaces), most common first.
- Number of correct answers, for questions that have a correct answer.
- A table with one row per student: their answer to each question, their progress (for
  example 4 of 10), and their score.
- **Hide results:** hides the answer counts on the teacher's screen, for projecting. It is
  saved on the server, so every screen the teacher has open agrees.
- **Hide names:** hides student names on the teacher's screen. It is saved in that browser
  only.
- **Remove a student:** their screen goes back to the join screen and they drop out of the
  results. They can join again unless the room is locked.

### Reports

- See all ended activities, newest first, for all rooms or one room. Each shows the date,
  room, type, quiz title, number of students and average score.
- Open a report to see the final results table and per-question summaries.
- Download a report as a CSV file that opens in Excel or Google Sheets. Columns: `name`,
  `joined_at`, `finished_at`, `score`, `total_possible`, `percent`, then one column per
  question with the student's answer. Times are in the teacher's time zone. Repeated names
  get "(2)", "(3)" and so on. Cells that a spreadsheet could run as a formula are made
  safe.
- The score is the number of correct answers. Questions with no correct answer do not
  count towards the total.
- Delete a report. A running activity must be ended first.

## For students

### Joining

- Open the site, type the room code and a display name (1 to 40 characters). No account,
  email or app install is needed.
- The room code ignores upper/lower case and spaces.
- Clear messages when the room does not exist or is locked.
- If no activity is running yet, the student waits on a waiting screen. When the teacher
  starts something, the student is moved in automatically with the same name.

### Answering

- Answer multiple choice, true/false and short answer questions (up to 500 characters).
- Change an answer until it is locked: when the teacher moves on, when the student
  presses Finish, when the activity ends, or right away if feedback is on.
- If saving fails, the app keeps retrying on its own. Retrying never creates a duplicate
  answer.
- Student-paced quizzes: move between questions freely, then press Finish.
- With feedback on, see right or wrong, the correct answer and the explanation after each
  answer.

### Staying connected

- Refreshing the page, closing and reopening the tab, or losing Wi-Fi for a moment brings
  the student back to the same question with their answers intact.
- If the live-update connection is blocked or down, the app checks for changes every few
  seconds instead.

### Leaving

- Press "Leave room" to clear the saved name and session for that room.
- A student the teacher removes is sent back to the join screen.

## Planned (not yet built)

None of these exist today, and none has a release date. Some are listed as out of scope
for the first version in the product spec.

- Teacher account deletion and data export.
- Automatic deletion of old results (data retention limits).
- Changing a teacher's name, email or password while logged in (only a reset by email
  exists).
- Sending account emails through a college's own mail server.
- Timers, question images or other media, and question banks.
- Leaderboards or game modes.
- Class rosters, student accounts, and LMS or Google Classroom integration.
- Rooms shared by several teachers, and school or admin accounts.
- Signing in with Google or another provider.
- Shuffling the order of answer choices.
