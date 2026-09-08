import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { verifyConsoleCredentials } from "@/lib/auth/credentials";

export const { auth, handlers, signIn, signOut } = NextAuth({
  pages: {
    signIn: "/sign-in",
  },
  providers: [
    Credentials({
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      authorize: (credentials) => verifyConsoleCredentials(credentials),
    }),
  ],
  session: {
    strategy: "jwt",
  },
  trustHost: true,
  callbacks: {
    session({ session, token }) {
      return {
        ...session,
        subject: token.sub ?? null,
      };
    },
  },
});
