import { useEffect, useRef } from "react";
import { toast } from "sonner";

const IS_TAURI = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

export function UpdateManager() {
    const checkAttempted = useRef(false);

    useEffect(() => {
        // Only run update check in Tauri environment
        if (!IS_TAURI) return;

        // Only run once on mount
        if (checkAttempted.current) return;
        checkAttempted.current = true;

        const checkForUpdates = async () => {
            try {
                const { check } = await import(/* @vite-ignore */ "@tauri-apps/plugin-updater");
                const { relaunch } = await import(/* @vite-ignore */ "@tauri-apps/plugin-process");

                // check() returns an Update object if an update is available, null otherwise.
                const update = await check();

                if (update) {
                    console.log(`Update available: ${update.version}`);

                    toast.info(`New version ${update.version} is available`, {
                        description: "An update is ready to be installed.",
                        duration: Infinity,
                        action: {
                            label: "Update and Restart",
                            onClick: async () => {
                                const id = toast.loading("Downloading and installing update...");
                                try {
                                    await update.downloadAndInstall();
                                    toast.success("Update installed successfully. Restarting...", { id });
                                    // Wait a bit so the user can see the success message
                                    setTimeout(async () => {
                                        await relaunch();
                                    }, 1500);
                                } catch (error) {
                                    console.error("Update failed:", error);
                                    toast.error("Failed to install update. Please try again later.", { id });
                                }
                            },
                        },
                    });
                }
            } catch (error) {
                // Silent error if check fails (e.g. offline or no updater configured)
                console.error("Failed to check for updates:", error);
            }
        };

        void checkForUpdates();
    }, []);

    return null;
}
