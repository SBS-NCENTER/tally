@echo off
cd /d "%~dp0"
echo 패키지 확인 중...
pip install flask google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client icalendar requests --quiet
echo 서버 시작 중...
python tally_server.py
pause
