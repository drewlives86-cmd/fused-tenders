# FUSED Tender Intelligence

Runs every day on GitHub, pulls Find a Tender + Contracts Finder, scores electrical-compliance
opportunities and publishes a dashboard at https://YOUR-USERNAME.github.io/REPO-NAME/

## One-time setup (about 15 minutes)
1. Create a free account at github.com.
2. Click + > New repository. Name it e.g. `fused-tenders`. Set it to Public (free Pages needs Public;
   the data is all public procurement data anyway). Do not tick "Add a README".
3. On the new repo page click "uploading an existing file". Drag in the contents of this folder:
   `fetch_and_score.py`, `README.md` and the `.github` folder (the folder must keep the path
   `.github/workflows/daily.yml`). If the .github folder won't drag in, use "Add file > Create new file",
   type `.github/workflows/daily.yml` as the name and paste the file's contents. Commit.
4. Go to Settings > Pages > Build and deployment > Source, and choose **GitHub Actions**.
5. Go to the Actions tab > "Daily tender refresh" > Run workflow. Wait 1-3 minutes.
6. Your dashboard is now live at https://YOUR-USERNAME.github.io/fused-tenders/ . Bookmark it.

It then refreshes itself at 05:00 UTC every day. Change the schedule in `daily.yml`, and the
look-back window with `--days` in the same file.

## Notes
- If a daily run fails (e.g. a source is down) the previous version of the page stays up.
- Contracts Finder rate-limits; the script waits 5 minutes if it is blocked.
- To tune what counts as relevant, edit the keyword/CPV lists at the top of `fetch_and_score.py`.
