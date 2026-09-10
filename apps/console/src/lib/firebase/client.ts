"use client";

import { getApp, getApps, initializeApp, type FirebaseApp } from "firebase/app";
import { getAuth, type Auth } from "firebase/auth";

// PUBLIC BY DESIGN. The Firebase web API key identifies the project; it authorises nothing on
// its own, which is why it ships as plain environment and not as a Secret Manager reference.
// The gate that matters is CONSOLE_SIGNUP_ENABLED on the product API.
const config = {
  apiKey: process.env.NEXT_PUBLIC_FIREBASE_API_KEY,
  authDomain: process.env.NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN,
  projectId: process.env.NEXT_PUBLIC_FIREBASE_PROJECT_ID,
};

function app(): FirebaseApp {
  // Next re-executes modules across client navigations; initializeApp twice throws.
  return getApps().length ? getApp() : initializeApp(config);
}

export function firebaseAuth(): Auth {
  return getAuth(app());
}

export function firebaseIsConfigured(): boolean {
  return Boolean(config.apiKey && config.authDomain && config.projectId);
}
