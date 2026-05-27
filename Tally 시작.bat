@echo off
pushd "%~dp0"
echo 패키지 확인 중...
python -m pip install flask google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client --quiet
echo 서버 시작 중...
python tally_server.py
popd
pause
