# Simplified Structure — Everything in app.py

This matches the flat, single-file style from your sample code, but keeps
credentials in `.env` instead of hardcoding them (best practice, and your
DB password never appears in the code this way).

## What changed from before
- No more `models/` folder — `get_db_connection()` and `create_table()`
  are now directly inside `app.py`, just like your sample.
- Everything else (routes, templates, CSS) works exactly the same as
  the previous Step 2 package.

## How to use this
1. Extract into your project folder, overwriting `app.py`.
2. **Delete the `models/` folder** if you still have it from before —
   it's no longer used.
3. Your `.env` file stays the same (no new variables needed).
4. Run:
   ```powershell
   python app.py
   ```

Everything (login, register, forgot password, add/view/edit/delete
expenses, dashboard) works exactly as before — just organized in one
file now instead of split across `app.py` + `models/db.py`.
