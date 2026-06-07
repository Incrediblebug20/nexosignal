async function connectMetaMask() {
  const status = document.querySelector("[data-wallet-status]");
  if (!window.ethereum) {
    status.textContent = "MetaMask is not available in this browser.";
    status.className = "bad";
    return;
  }

  try {
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    const address = accounts && accounts[0];
    if (!address) {
      status.textContent = "No wallet address returned.";
      status.className = "bad";
      return;
    }

    const resp = await fetch("/wallet/metamask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address }),
    });
    const payload = await resp.json();
    if (!resp.ok || !payload.ok) {
      throw new Error(payload.error || "Wallet capture failed.");
    }

    status.textContent = `Connected: ${payload.address}`;
    status.className = "good";
  } catch (err) {
    status.textContent = err.message || "MetaMask connection failed.";
    status.className = "bad";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const button = document.querySelector("[data-connect-metamask]");
  if (button) button.addEventListener("click", connectMetaMask);
});
