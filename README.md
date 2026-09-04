# MediaFlow

A basic Django foundation for a smart media backlog and scheduling application. Users can track movies, TV, books, games, and other media, record their weekly availability and one-off commitments, and automatically build a schedule from their priorities.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Open `http://127.0.0.1:8000/`. The admin is at `/admin/`.

## Included

- Django authentication (register, sign in, sign out)
- Media backlog with priority, progress, status, and estimated duration
- Repeating weekly availability windows
- Dated commitments that block scheduling
- A simple greedy scheduler that fits high-priority items into open time
- Dashboard, forms, admin configuration, tests, and an initial migration

The scheduling logic lives in `planner/services.py`, making it easy to replace with a more sophisticated recommendation or optimization engine later.
