import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { authorizeFirebaseSession } from "@/lib/auth/firebase-session";

export const { auth, handlers, signIn, signOut } = NextAuth({
  pages: {
    signIn: "/sign-in",
  },
  providers: [
    Credentials({
      // The console never sees a password. Identity Platform verified it and minted this token;
      // the product API verifies the token. This provider only carries it between the two.
      credentials: {
        idToken: { label: "ID token", type: "text" },
      },
      authorize: (credentials) => authorizeFirebaseSession(credentials),
    }),
  ],
  session: {
    strategy: "jwt",
  },
  trustHost: true,
  callbacks: {
    jwt({ token, user }) {
      if (user) {
        token.organizationId = (user as { organizationId?: string }).organizationId ?? "";
        token.emailVerified = (user as { emailVerified?: boolean }).emailVerified ?? false;
      }
      return token;
    },
    session({ session, token }) {
      return {
        ...session,
        subject: token.sub ?? null,
        organizationId: (token.organizationId as string | undefined) ?? "",
        emailVerified: token.emailVerified === true,
      };
    },
  },
});
