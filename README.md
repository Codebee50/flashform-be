# Flashform

Flashform is a free, open-source classroom response app for live quizzes and polls. A
teacher starts a question or a quiz in a room. Students join from their phones or laptops
with the room code and a name. Students do not need an account.

This repository is the backend (Django API and WebSockets) and the main home for the
project's documentation. The frontend is a separate repository.

**Status:** prototype, under active development.

## Links

- Live app: https://flashform.live
- Frontend repo: https://github.com/Codebee50/flashform-fe
- API docs (live): https://flashformlive.up.railway.app/api/docs/

## At a glance

- Students join with a room code and a display name. No student accounts, emails or
  passwords.
- Quick Questions (multiple choice, true/false, short answer) with no preparation, plus
  saved quizzes run at the teacher's pace or the student's pace.
- Live results for the teacher: answer counts, per-student answers and scores.
- Reports for past activities, with CSV download.
- Answers are saved over plain HTTP and are safe to retry, so a flaky connection does not
  lose them.
- Teachers only ever see their own rooms, quizzes and results. Students never see other
  students' answers, and see correct answers only when the teacher turns feedback on.
- Tested with a 50-student load test (join, answer, drop and reconnect all at once).

## Documentation

- [Data & privacy](docs/privacy.md): what is stored about students and teachers, who
  can see it, and how to delete it.
- [Features](docs/features.md): what teachers and students can do.
- [Architecture](docs/architecture.md): how the parts fit together and how the app
  stays reliable.
- [Development](docs/development.md): running the stack locally, configuration, tests.

## Quick start

Needs Docker with Docker Compose.

```bash
git clone https://github.com/Codebee50/flashform-be.git
cd flashform-be
cp .env.example .env
docker compose up
```

The API is then at http://localhost:8000/api and its docs at
http://localhost:8000/api/docs/. See [docs/development.md](docs/development.md) for
details.

## Author

Kyrian Onuh, Computer Science graduate student at Lehman College.

- Email: onuhudoudo@gmail.com
- GitHub: https://github.com/codebee50
- LinkedIn: https://www.linkedin.com/in/udokyrian/

## License

MIT. See [LICENSE](LICENSE).
