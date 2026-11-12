#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
if [ -e .env ]; then
  printf '%s\n' '.env already exists; kept unchanged.'
  exit 0
fi
umask 077
password=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
sed "s/replace_with_generated_hex_password/$password/" .env.example > .env
printf '%s\n' 'Created local .env. The generated database password was not printed.'
