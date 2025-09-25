from flask import Flask, render_template, request, redirect, url_for, flash
import os
import requests
import zipfile
import shutil
from ai_validator import validate_project
#from script.frontend import validate_frontend
#Sfrom script.backend import validate_backend

EXTRACT_FOLDER = "./extracted"
os.makedirs(EXTRACT_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = 'your_secret'


def get_extracted_root_dir(extract_path):
    """
    If the path exists and is a directory, return it.
    """
    if os.path.exists(extract_path) and os.path.isdir(extract_path):
        return extract_path
    return None


def get_repo_name_from_url(repo_url):
    """
    Extract repository name from the GitHub URL.
    Example: https://github.com/user/repo -> repo
    """
    repo_name = repo_url.rstrip('/').split('/')[-1]
    return repo_name


@app.route('/')
def home():
    return render_template("home.html")


@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        repo_url = request.form.get('repo_url', '').strip()
        required_input = request.form.get('required_items', '').strip()

        if not repo_url.startswith("https://github.com/"):
            flash("Please enter a valid GitHub repository URL starting with https://github.com/", "danger")
            return redirect(request.url)

        if not required_input:
            flash("Please enter at least one required file/folder to check.", "danger")
            return redirect(request.url)

        return redirect(url_for('validate', repo=repo_url, required=required_input))

    return render_template("upload.html", repo_url='', required_items='')


@app.route('/validate')
def validate():
    repo_url = request.args.get('repo', '')
    required_input = request.args.get('required', '')

    if not repo_url or not required_input:
        flash("Missing repository URL or required files/folders data.", "danger")
        return redirect(url_for('upload'))

    required_items = [item.strip() for item in required_input.split(',') if item.strip()]

    repo_name = get_repo_name_from_url(repo_url)
    extract_path = os.path.join(EXTRACT_FOLDER, repo_name)

    # Remove previous extracted repo folder
    if os.path.exists(extract_path):
        shutil.rmtree(extract_path)
    os.makedirs(extract_path, exist_ok=True)

    if repo_url.endswith('/'):
        repo_url = repo_url[:-1]

    zip_url = f"{repo_url}/archive/refs/heads/main.zip"

    try:
        resp = requests.get(zip_url, stream=True)
        if resp.status_code != 200:
            flash("Failed to download zip. Check if the repository and branch exist.", "danger")
            return redirect(url_for('upload'))

        temp_zip_path = os.path.join(EXTRACT_FOLDER, "repo.zip")
        with open(temp_zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        # Extract files into extract_path, removing top-level folder from zip
        with zipfile.ZipFile(temp_zip_path, "r") as zip_ref:
            for member in zip_ref.namelist():
                parts = member.split('/', 1)
                if len(parts) == 2:
                    member_path = parts[1]
                else:
                    member_path = parts[0]

                target_path = os.path.join(extract_path, member_path)
                if member.endswith('/'):
                    os.makedirs(target_path, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target_path), exist_ok=True)
                    with zip_ref.open(member) as source, open(target_path, "wb") as target:
                        target.write(source.read())

        os.remove(temp_zip_path)

        project_root = get_extracted_root_dir(extract_path)

        missing = []
        for item in required_items:
            if not os.path.exists(os.path.join(project_root, item)):
                missing.append(item)

        contents = []
        for root, dirs, files in os.walk(project_root):
            for name in dirs + files:
                relative_path = os.path.relpath(os.path.join(root, name), project_root)
                contents.append(relative_path)

        return render_template("validation.html", repo_url=repo_url,
                               required_items=required_items, missing=missing, contents=contents,repo_name=repo_name)

    except Exception as e:
        flash(f"Error occurred during processing: {str(e)}", "danger")
        return redirect(url_for('upload'))


@app.route('/validate/dev', methods=['GET', 'POST'])
def dev_validate():
    """
    Developer validation on the already extracted repo inside EXTRACT_FOLDER.
    User optionally specifies subfolder and frontend/backend role.
    """
    if request.method == 'POST':
        subfolder = request.form.get('subfolder', '').strip()
        dev_type = request.form.get('dev_type', '').strip()
        repo_name = request.form.get('repo_name', '').strip()

        if not repo_name:
            flash("Repository name must be specified.", "danger")
            return redirect(request.url)

        if dev_type not in ('frontend', 'backend'):
            flash("Please select a valid developer type: frontend or backend.", "danger")
            return redirect(request.url)

        base_root = os.path.join(EXTRACT_FOLDER, repo_name)
        if not os.path.isdir(base_root):
            flash(f"Extracted repository folder '{repo_name}' does not exist.", "danger")
            return redirect(request.url)

        project_root = base_root
        if subfolder:
            candidate_path = os.path.join(base_root, subfolder)
            if os.path.isdir(candidate_path):
                project_root = candidate_path
            else:
                flash(f"Specified subfolder '{subfolder}' does not exist in extracted repository.", "danger")
                return redirect(request.url)

        errors = []
        missing = []

        if dev_type == 'frontend':
            # Check for index.html or package.json
            if not (os.path.exists(os.path.join(project_root, 'index.html')) or os.path.exists(os.path.join(project_root, 'package.json'))):
                missing.append("index.html or package.json")
        else:
            # backend
            if not (os.path.exists(os.path.join(project_root, 'requirements.txt')) or os.path.exists(os.path.join(project_root, 'app.py'))):
                missing.append("requirements.txt or app.py")

        contents = []
        for root, dirs, files in os.walk(project_root):
            for name in dirs + files:
                relative_path = os.path.relpath(os.path.join(root, name), project_root)
                contents.append(relative_path)

        return render_template("validation.html", dev_type=dev_type,
                               missing=missing, errors=errors, contents=contents,
                               subfolder=subfolder, repo_name=repo_name)

    return render_template("validation.html", subfolder=None, repo_name=None)

@app.route('/validate/ai', methods=['POST'])
def validate_ai():
    repo_name = request.form.get('repo_name', '').strip()
    role = request.form.get('role', 'intern').strip()
    subfolder = request.form.get('subfolder', '').strip()

    if not repo_name:
        flash("repo_name required", "danger")
        return redirect(url_for('upload'))

    project_root = os.path.join(EXTRACT_FOLDER, repo_name)
    if not os.path.isdir(project_root):
        flash("Extracted repo not found", "danger")
        return redirect(url_for('upload'))


    project_root = os.path.dirname(os.path.abspath(__file__))

    if subfolder:
        candidate_path = os.path.join(base_root, subfolder)
        if os.path.isdir(candidate_path):
            project_root = candidate_path
        else:
            flash(f"Subfolder '{subfolder}' not found in repository", "danger")
            return redirect(url_for('validate', repo=repo_name, required=''))

    try:
        res = validate_project(project_root, 'criteria.json', role)
        # res['result'] is the parsed JSON from model
        return render_template('ai_report.html', report=res['result'], raw=res['raw'], files_sent=res['files_sent_count'])
    except Exception as e:
        flash(f"AI validation failed: {e}", "danger")
        return redirect(url_for('dev_validate'))

if __name__ == '__main__':
    app.run(debug=True)
