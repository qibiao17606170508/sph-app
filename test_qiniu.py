from qiniu import Auth, put_file
ak = "test_ak"
sk = "test_sk"
bucket = "test_bucket"
key = "releases/test.zip"
q = Auth(ak, sk)
token = q.upload_token(bucket, key, 3600)
print(token)
