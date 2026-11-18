import { redirect } from "next/navigation";

// "/" lives INSIDE the (dashboard) group on purpose: a next.config redirect here would answer
// unauthenticated requests with /runs before the layout's session gate ran, and the deploy's
// console check asserts GET / -> /sign-in in one hop. As a page the order is right again -
// the gate sends visitors to sign-in, and a signed-in user lands on the one work surface.
export default function RootPage() {
  redirect("/runs");
}
