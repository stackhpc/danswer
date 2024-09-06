"use client";

import { usePopup } from "@/components/admin/connectors/Popup";
import { basicLogin, basicSignup } from "@/lib/user";
import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { Spinner } from "@/components/Spinner";

export function HeaderLoginLoading({
    user, groups
}: {
    user: string;
    groups: string[];
}) {
    console.log(user, groups);

    const router = useRouter();
    const { popup, setPopup } = usePopup();
    // NOTE: As long as Danswer is only ever exposed
    // via Zenith then these credentials are irrelevant.
    const email = `${user}@default.com`;
    const password = `not-used-${user}`
    const role = groups.includes("/admins") ? "admin" : "basic"

    async function tryLogin() {
        // TODO: Update user role here if groups have changed?

        // TODO: Use other API endpoints here to update user roles
        // and check for existence instead of attempting sign up
        // Endpoints:
        // - /api/manage/users
        // - /api/manage/promote-user-to-admin (auth required)
        // - /api/manage/demote-admin-to-user (auth required)

        // signup every time.
        // Ensure user exists
        const response = await basicSignup(email, password, role);
        if (!response.ok) {
            const errorDetail = (await response.json()).detail;

            if (errorDetail !== "REGISTER_USER_ALREADY_EXISTS") {
                setPopup({
                    type: "error",
                    message: `Failed to sign up - ${errorDetail}`,
                });
            }
        }
        // Login as user
        const loginResponse = await basicLogin(email, password);
        if (loginResponse.ok) {
            router.push("/");
        } else {
            const errorDetail = (await loginResponse.json()).detail;
            setPopup({
                type: "error",
                message: `Failed to login - ${errorDetail}`,
            });
        }
    }

    useEffect(() => {
        tryLogin()
    }, []);

    return (
        <>
            {popup}
            <Spinner />
        </>
    );
}
