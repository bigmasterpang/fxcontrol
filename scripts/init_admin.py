#!/usr/bin/env python3
"""
CLI tool to create or reset the administrator password for FX Control.
"""
import sys
import os
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "server"))
import models

def main():
    parser = argparse.ArgumentParser(description="Reset or create admin user for FX Control")
    parser.add_argument("--username", default="admin", help="Admin username")
    parser.add_argument("--password", default="admin123", help="Admin password")
    parser.add_argument("--db", default=None, help="Path to fx.db")
    args = parser.parse_args()

    models.init_db(args.db)
    u = models.get_user_by_username(args.username)
    if u:
        models.reset_user_password(u["id"], args.password)
        print(f"[+] Password for existing user '{args.username}' has been successfully reset!")
    else:
        models.create_user(args.username, args.password, ip="127.0.0.1", role="admin")
        print(f"[+] Created new administrator '{args.username}' successfully!")

if __name__ == '__main__':
    main()
