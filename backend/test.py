import requests

token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhcm5hYi5tdHJAZ21haWwuY29tIiwidWlkIjoxLCJleHAiOjE3ODQ2MTg1MTJ9.AQ_bsPEoZvRxNvNVOfD8566N-VbTgxrgJlU1Qv3SYAs"

url = "http://localhost:8000/income/regime?index=NIFTY"

headers = {
    "Authorization": f"Bearer {token}"
}

response = requests.get(url, headers=headers)

print(response.status_code)
print(response.json())