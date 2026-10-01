# Code signing

`build.sh` signs `quickask.app` with the certificate named **llm-popup signing**
from the login keychain. It is self-signed, valid until September 2036, and exists
for one reason: macOS ties privacy grants (Accessibility for window titles,
Automation for System Events) to the app's signing identity. An ad-hoc signature
changes on every build and silently loses those grants; a fixed certificate keeps
them across rebuilds. The certificate predates the app's rename to quickask and
keeps its original name, since a certificate's name cannot be changed.

If the certificate is missing, `build.sh` falls back to ad-hoc signing and warns.
`QUICKASK_SIGN_IDENTITY` overrides the certificate name.

## Recreate it (new machine)

```sh
cat > cs.cnf <<'CNF'
[ req ]
distinguished_name = dn
x509_extensions = ext
prompt = no
[ dn ]
CN = llm-popup signing
O = ian
[ ext ]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
subjectKeyIdentifier = hash
CNF
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes -config cs.cnf -keyout key.pem -out cert.pem
openssl pkcs12 -export -inkey key.pem -in cert.pem -name "llm-popup signing" -out id.p12 -passout pass:x -legacy
security import id.p12 -k ~/Library/Keychains/login.keychain-db -P x -T /usr/bin/codesign -T /usr/bin/security
security add-trusted-cert -r trustRoot -p codeSign -k ~/Library/Keychains/login.keychain-db cert.pem
rm key.pem id.p12 cs.cnf
security find-identity -v -p codesigning   # should list "llm-popup signing"
```

After the first build with a new certificate, grant Accessibility to quickask once
more in System Settings > Privacy & Security > Accessibility.

## Remove it

```sh
security delete-identity -c "llm-popup signing" ~/Library/Keychains/login.keychain-db
```

Builds then fall back to ad-hoc signing.
