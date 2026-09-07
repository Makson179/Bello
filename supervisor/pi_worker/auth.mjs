#!/usr/bin/env node

import { authMain } from "./src/auth-cli.mjs";

process.exitCode = await authMain();
