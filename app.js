(() => {
  const telegram = window.Telegram?.WebApp;
  const initData = telegram?.initData || "";
  const modal = document.querySelector("#modal");
  const modalBody = document.querySelector("#modal-body");
  let bet = 50;
  let choice = "Heads";
  let busy = false;
  let appUrl = "";
  let currentUser = null;
  let activeRoomCode = "";
  let roomPoll = null;
  let roomBusy = false;

  async function request(path, payload) {
    const response = await fetch(path, {
      method: payload ? "POST" : "GET",
      headers: {
        Authorization: `tma ${initData}`,
        ...(payload ? { "Content-Type": "application/json" } : {}),
      },
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    return data;
  }

  function showToast(text) {
    const element = document.querySelector("#toast");
    element.textContent = text;
    element.classList.add("show");
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => element.classList.remove("show"), 2600);
  }

  function updateBalance(balance) {
    document.querySelector("#balance").textContent = balance.toLocaleString();
  }

  function updateGreeting(name) {
    document.querySelector("#greeting").textContent = `Good evening, ${name}`;
  }

  function createRoomJoinForm() {
    const form = document.createElement("form");
    form.className = "room-join";
    const label = document.createElement("label");
    label.htmlFor = "room-code-input";
    label.textContent = "Have a room code?";
    const controls = document.createElement("div");
    controls.className = "room-join-controls";
    const input = document.createElement("input");
    input.id = "room-code-input";
    input.name = "room-code";
    input.autocomplete = "off";
    input.autocapitalize = "characters";
    input.maxLength = 8;
    input.pattern = "[A-Fa-f0-9]{8}";
    input.placeholder = "8-character code";
    input.title = "Enter the 8-character room code";
    input.required = true;
    const button = document.createElement("button");
    button.className = "secondary";
    button.type = "submit";
    button.textContent = "Join room";
    controls.append(input, button);
    form.append(label, controls);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      joinRoom(input.value.trim().toUpperCase());
    });
    return form;
  }

  function promptForName(suggestedName, firstRun = false) {
    return new Promise((resolve) => {
      document.querySelector("#modal-title").textContent = firstRun ? "What should we call you?" : "Change your display name";
      modalBody.replaceChildren();
      const form = document.createElement("form");
      form.className = "name-form";
      const label = document.createElement("label");
      label.htmlFor = "display-name";
      label.textContent = "Your display name";
      const input = document.createElement("input");
      input.id = "display-name";
      input.name = "display-name";
      input.value = suggestedName || "";
      input.maxLength = 24;
      input.autocomplete = "nickname";
      input.required = true;
      const submit = document.createElement("button");
      submit.className = "primary";
      submit.type = "submit";
      submit.textContent = "Continue";
      form.append(label, input, submit);
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          const account = await request("/api/me/name", { name: input.value.trim() });
          currentUser = account.user;
          updateGreeting(currentUser.display_name);
          modal.dataset.locked = "";
          modal.querySelector(".close").hidden = false;
          modal.classList.remove("open");
          resolve();
        } catch (error) {
          showToast(error.message);
          submit.disabled = false;
        }
      });
      modal.querySelector(".close").hidden = true;
      modal.dataset.locked = "name";
      modalBody.append(form);
      modal.classList.add("open");
      input.focus();
    });
  }

  function setupRoomView() {
    document.querySelector(".hero > p:last-of-type").textContent = "Play quick virtual-coin games or create a private room to invite friends.";
    document.querySelector(".hero [data-game='poker']").textContent = "Create poker table";
    document.querySelector(".games [data-game='poker']").textContent = "Play poker →";
    document.querySelector(".games article:nth-child(2) p").textContent = "Call the toss · 20 sec";
    document.querySelector(".games article:last-child p").textContent = "Texas Hold'em · 2–8";
    const sectionHead = Array.from(document.querySelectorAll(".section-head"))
      .find((element) => element.querySelector("h2")?.textContent === "Your circle");
    if (sectionHead) {
      sectionHead.querySelector("h2").textContent = "Your rooms";
      sectionHead.querySelector("button")?.remove();
    }
    const columns = document.querySelector(".columns");
    const roomsPanel = document.createElement("div");
    roomsPanel.className = "panel";
    roomsPanel.dataset.myRooms = "";
    const roomsHeading = document.createElement("h3");
    roomsHeading.textContent = "Your invite rooms";
    const roomsEmpty = document.createElement("p");
    roomsEmpty.textContent = "No rooms yet. Create one to invite friends.";
    const createButton = document.createElement("button");
    createButton.className = "primary";
    createButton.dataset.game = "poker";
    createButton.textContent = "Create a poker table";
    roomsPanel.append(roomsHeading, roomsEmpty, createButton, createRoomJoinForm());
    const invitePanel = document.createElement("div");
    invitePanel.className = "panel";
    const inviteHeading = document.createElement("h3");
    inviteHeading.textContent = "Invite friends";
    const inviteText = document.createElement("p");
    inviteText.textContent = "Invite friends anywhere in the world with your Soto room link or code.";
    const inviteButton = document.createElement("button");
    inviteButton.className = "primary";
    inviteButton.dataset.invite = "";
    inviteButton.textContent = "Share Soto";
    invitePanel.append(inviteHeading, inviteText, inviteButton);
    columns.replaceChildren(roomsPanel, invitePanel);
    const friendsTab = Array.from(document.querySelectorAll(".bottom button"))
      .find((button) => button.textContent.includes("Friends"));
    if (friendsTab?.lastChild) friendsTab.lastChild.textContent = "Rooms";
  }

  async function loadRooms() {
    const { rooms } = await request("/api/me/rooms");
    const panel = document.querySelector("[data-my-rooms]");
    panel.replaceChildren();
    const heading = document.createElement("h3");
    heading.textContent = "Your invite rooms";
    panel.append(heading);
    if (!rooms.length) {
      const emptyState = document.createElement("p");
      emptyState.textContent = "No rooms yet. Create one to invite friends.";
      const createButton = document.createElement("button");
      createButton.className = "primary";
      createButton.dataset.game = "poker";
      createButton.textContent = "Create a poker table";
      panel.append(emptyState, createButton, createRoomJoinForm());
      return;
    }
    for (const room of rooms) {
      const row = document.createElement("div");
      row.className = "row";
      const avatar = document.createElement("span");
      avatar.className = "avatar";
      avatar.textContent = room.code.slice(0, 2);
      const details = document.createElement("div");
      details.className = "person";
      const title = document.createElement("strong");
      title.textContent = `Room ${room.code}`;
      const subtitle = document.createElement("span");
      subtitle.textContent = `${room.players} ${room.players === 1 ? "player" : "players"} · hosted by ${room.owner_name}`;
      const inviteButton = document.createElement("button");
      inviteButton.className = "small";
      inviteButton.dataset.shareRoom = "";
      inviteButton.dataset.url = room.share_url;
      inviteButton.textContent = "Invite";
      const openButton = document.createElement("button");
      openButton.className = "small";
      openButton.dataset.openRoom = room.code;
      openButton.textContent = "Open table";
      details.append(title, subtitle);
      row.append(avatar, details, openButton, inviteButton);
      panel.append(row);
    }
    panel.append(createRoomJoinForm());
  }

  function updateBet() {
    const label = document.querySelector("#bet");
    if (label) label.textContent = bet;
    for (const [selector, action] of [["#spin", "Spin"], ["#flip-btn", "Flip"], ["#deal", "Deal a hand"]]) {
      const button = document.querySelector(selector);
      if (button) button.textContent = `${action} · ${bet} coins`;
    }
  }

  function setResult(text) {
    const result = document.querySelector("#result");
    if (result) result.textContent = text;
  }

  function showProfile() {
    document.querySelector("#modal-title").textContent = "Your profile";
    modalBody.replaceChildren();
    const name = document.createElement("h3");
    name.textContent = currentUser?.display_name || currentUser?.first_name || "Telegram account";
    const description = document.createElement("p");
    description.textContent = currentUser ? "Signed in with Telegram." : "Open Soto from Telegram to view your account.";
    const balance = document.createElement("p");
    balance.textContent = `Play coins: ${document.querySelector("#balance").textContent}`;
    const editName = document.createElement("button");
    editName.className = "secondary";
    editName.id = "edit-name";
    editName.textContent = "Change name";
    modalBody.append(name, description, balance, editName);
    modal.classList.add("open");
  }

  function share(url, text) {
    if (!url) {
      showToast("Set TELEGRAM_BOT_USERNAME to create share links.");
      return;
    }
    const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;
    if (telegram?.openTelegramLink) telegram.openTelegramLink(shareUrl);
    else window.location.assign(shareUrl);
  }

  async function play(game) {
    if (busy) return;
    busy = true;
    const button = document.querySelector(game === "slots" ? "#spin" : game === "coin" ? "#flip-btn" : "#deal");
    if (button) button.disabled = true;
    try {
      const data = await request(`/api/games/${game}`, { bet, choice });
      updateBalance(data.balance);
      if (game === "coin") {
        const coin = document.querySelector("#flip");
        coin.textContent = data.result[0];
        coin.classList.remove("spin");
        void coin.offsetWidth;
        coin.classList.add("spin");
        setResult(data.won ? `${data.result}! You win ${data.payout} play coins.` : `${data.result}. Your call was ${choice}.`);
      } else if (game === "slots") {
        data.reels.forEach((symbol, index) => {
          document.querySelector(`#r${index + 1}`).textContent = symbol;
        });
        setResult(data.payout === bet * 5 ? `Jackpot! ${data.payout} play coins back.` : data.won ? `A pair! ${data.payout} play coins back.` : "No match this time.");
      } else {
        const stage = document.querySelector(".stage");
        const cards = (values) => values.map((value) => `<span class="reel">${value === 11 ? "A" : value}</span>`).join("");
        stage.innerHTML = `${cards(data.player)}<span style="font-size:22px">vs</span>${cards(data.dealer)}`;
        setResult(data.push ? `Push. Your ${data.player_score} ties the dealer; your bet was returned.` : data.won ? `You win with ${data.player_score} against ${data.dealer_score}!` : `Dealer wins with ${data.dealer_score}; you had ${data.player_score}.`);
      }
    } catch (error) {
      showToast(error.message);
    } finally {
      busy = false;
      if (button) button.disabled = false;
    }
  }

  function blackjackCard(card, hidden = false) {
    const element = document.createElement("div");
    element.className = `blackjack-card${hidden ? " is-hidden" : ""}${!hidden && (card.suit === "♥" || card.suit === "♦") ? " is-red" : ""}`;
    if (hidden) {
      element.setAttribute("aria-label", "Hidden dealer card");
      return element;
    }
    const rank = document.createElement("span");
    rank.textContent = card.rank;
    const suit = document.createElement("span");
    suit.textContent = card.suit;
    element.append(rank, suit);
    element.setAttribute("aria-label", `${card.rank} of ${card.suit}`);
    return element;
  }

  function renderBlackjack(hand) {
    updateBalance(hand.balance);
    modalBody.replaceChildren();
    const gameBoard = document.createElement("div");
    gameBoard.className = "blackjack-game";
    const dealer = document.createElement("section");
    dealer.className = "blackjack-hand dealer-hand";
    const dealerHeading = document.createElement("div");
    dealerHeading.className = "blackjack-hand-heading";
    const dealerName = document.createElement("strong");
    dealerName.textContent = "Dealer";
    const dealerScore = document.createElement("span");
    dealerScore.textContent = hand.dealer_hidden ? `Showing ${hand.dealer_score}` : `Total ${hand.dealer_score}`;
    dealerHeading.append(dealerName, dealerScore);
    const dealerCards = document.createElement("div");
    dealerCards.className = "blackjack-cards";
    hand.dealer_cards.forEach((card) => dealerCards.append(blackjackCard(card)));
    if (hand.dealer_hidden) dealerCards.append(blackjackCard(null, true));
    dealer.append(dealerHeading, dealerCards);

    const versus = document.createElement("div");
    versus.className = "blackjack-versus";
    versus.textContent = "VS";

    const player = document.createElement("section");
    player.className = "blackjack-hand player-hand";
    const playerHeading = document.createElement("div");
    playerHeading.className = "blackjack-hand-heading";
    const playerName = document.createElement("strong");
    playerName.textContent = currentUser?.display_name || "You";
    const playerScore = document.createElement("span");
    playerScore.textContent = `Total ${hand.player_score}`;
    playerHeading.append(playerName, playerScore);
    const playerCards = document.createElement("div");
    playerCards.className = "blackjack-cards";
    hand.player_cards.forEach((card) => playerCards.append(blackjackCard(card)));
    player.append(playerHeading, playerCards);

    const status = document.createElement("p");
    status.className = `blackjack-status ${hand.status}`;
    if (hand.status === "playing") {
      status.textContent = "Your move. Hit for another card or stand to hold.";
    } else if (hand.status === "won") {
      status.textContent = `You win. ${hand.payout} play coins returned.`;
    } else if (hand.status === "push") {
      status.textContent = `Push. Your ${hand.bet} coin bet was returned.`;
    } else {
      status.textContent = "Dealer wins this hand. Try another round.";
    }

    const actions = document.createElement("div");
    actions.className = "blackjack-actions";
    if (hand.status === "playing") {
      const hit = document.createElement("button");
      hit.className = "primary";
      hit.id = "blackjack-hit";
      hit.textContent = "Hit";
      const stand = document.createElement("button");
      stand.className = "secondary";
      stand.id = "blackjack-stand";
      stand.textContent = "Stand";
      actions.append(hit, stand);
    } else {
      const newHand = document.createElement("button");
      newHand.className = "primary";
      newHand.id = "deal";
      newHand.textContent = `New hand · ${bet} coins`;
      actions.append(newHand);
    }
    gameBoard.append(dealer, versus, player, status, actions);
    modalBody.append(gameBoard);
  }

  async function blackjackAction(action) {
    if (busy) return;
    busy = true;
    modalBody.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try {
      const payload = action === "start" ? { bet } : {};
      const hand = await request(`/api/games/blackjack/${action}`, payload);
      renderBlackjack(hand);
    } catch (error) {
      showToast(error.message);
      modalBody.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    } finally {
      busy = false;
    }
  }

  async function createRoom() {
    try {
      const room = await request("/api/rooms", {});
      await loadRooms();
      await openRoom(room.code);
    } catch (error) {
      showToast(error.message);
    }
  }

  async function joinRoom(code) {
    try {
      const room = await request("/api/rooms/join", { code });
      await loadRooms();
      await openRoom(room.code);
    } catch (error) {
      showToast(error.message);
    }
  }

  function pokerCard(card, hidden = false) {
    const element = document.createElement("span");
    element.className = `poker-card${hidden ? " is-hidden" : ""}${!hidden && ["♥", "♦"].includes(card.suit) ? " is-red" : ""}`;
    element.textContent = hidden ? "🂠" : `${({ 11: "J", 12: "Q", 13: "K", 14: "A" })[card.rank] || card.rank}${card.suit}`;
    return element;
  }

  function roomButton(label, className, id) {
    const button = document.createElement("button");
    button.className = className;
    button.textContent = label;
    button.id = id;
    return button;
  }

  function renderPokerRoom(data) {
    const game = data.game;
    document.querySelector("#modal-title").textContent = `Poker table · ${data.code}`;
    modalBody.replaceChildren();
    const intro = document.createElement("p");
    intro.textContent = "Texas Hold’em · 2–8 players · free table chips (500 each)";
    modalBody.append(intro);

    const memberList = document.createElement("div");
    memberList.className = "poker-players";
    const players = game?.players || data.members.map((member) => ({
      telegram_id: member.telegram_id,
      name: member.name,
      stack: 500,
      hole_cards: [],
    }));
    for (const player of players) {
      const seat = document.createElement("div");
      seat.className = "poker-player";
      const name = document.createElement("strong");
      name.textContent = player.name;
      const stack = document.createElement("span");
      stack.textContent = `${player.stack} chips`;
      const cards = document.createElement("div");
      cards.className = "poker-cards";
      if (game && game.status === "playing" && player.telegram_id !== currentUser.id) {
        cards.append(pokerCard(null, true), pokerCard(null, true));
      } else {
        (player.hole_cards || []).forEach((card) => cards.append(pokerCard(card)));
      }
      if (game?.button === players.indexOf(player)) {
        const dealer = document.createElement("small");
        dealer.textContent = "Dealer";
        seat.append(dealer);
      }
      seat.append(name, stack, cards);
      if (game?.status === "playing" && game.current_player_id === player.telegram_id) {
        seat.classList.add("is-turn");
      }
      memberList.append(seat);
    }
    modalBody.append(memberList);

    if (!game) {
      const waiting = document.createElement("p");
      waiting.textContent = `${data.players} of 8 seats filled. Share the room link, then the host can deal when at least two players have joined.`;
      modalBody.append(waiting);
    } else {
      const board = document.createElement("div");
      board.className = "poker-board";
      const pot = document.createElement("strong");
      pot.textContent = game.status === "playing" ? `Pot · ${game.pot} chips` : `Last pot · ${game.last_pot || 0} chips`;
      board.append(pot);
      const community = document.createElement("div");
      community.className = "poker-cards community-cards";
      (game.board || []).forEach((card) => community.append(pokerCard(card)));
      board.append(community);
      modalBody.append(board);
      const message = document.createElement("p");
      if (game.status === "playing") {
        const turnPlayer = players.find((player) => player.telegram_id === game.current_player_id);
        message.textContent = game.current_player_id === currentUser.id
          ? `Your turn · ${game.street} · call ${Math.max(0, game.current_bet - players.find((player) => player.telegram_id === currentUser.id).current_bet)}`
          : `Waiting for ${turnPlayer?.name || "the next player"} · ${game.street}`;
      } else if (game.status === "hand_complete") {
        message.textContent = `Hand complete. ${game.winners.map((winner) => {
          const player = players.find((seat) => seat.telegram_id === winner.telegram_id);
          return `${player?.name || "Player"} won ${winner.amount}`;
        }).join(" · ")}.`;
      } else {
        message.textContent = game.status === "finished"
          ? "Table finished: only one player still has chips."
          : "The hand is ready.";
      }
      modalBody.append(message);
    }

    const actions = document.createElement("div");
    actions.className = "poker-actions";
    const shareButton = roomButton("Invite friends", "primary", "share-room");
    shareButton.dataset.url = data.share_url || "";
    actions.append(shareButton);
    if (!game && data.owner_id === currentUser.id) {
      const startButton = roomButton("Deal first hand", "primary", "poker-start");
      startButton.disabled = data.players < 2;
      actions.append(startButton);
    } else if (game?.status === "hand_complete") {
      actions.append(roomButton("Deal next hand", "primary", "poker-start"));
    } else if (game?.status === "playing" && game.current_player_id === currentUser.id) {
      const player = players.find((seat) => seat.telegram_id === currentUser.id);
      const toCall = Math.max(0, game.current_bet - player.current_bet);
      actions.append(roomButton("Fold", "secondary", "poker-fold"));
      actions.append(roomButton(toCall ? `Call ${toCall}` : "Check", "primary", toCall ? "poker-call" : "poker-check"));
      const minimumRaiseTo = game.current_bet + game.min_raise;
      if (player.current_bet + player.stack >= minimumRaiseTo) {
        const raiseLabel = document.createElement("label");
        raiseLabel.htmlFor = "poker-raise-to";
        raiseLabel.textContent = "Raise to";
        const raiseInput = document.createElement("input");
        raiseInput.id = "poker-raise-to";
        raiseInput.type = "number";
        raiseInput.min = minimumRaiseTo;
        raiseInput.max = player.current_bet + player.stack;
        raiseInput.value = minimumRaiseTo;
        raiseInput.setAttribute("aria-label", "Raise to amount");
        const raiseButton = roomButton("Raise", "secondary", "poker-raise");
        actions.append(raiseLabel, raiseInput, raiseButton);
      }
    }
    modalBody.append(actions);
  }

  function stopRoomPolling() {
    if (roomPoll) clearInterval(roomPoll);
    roomPoll = null;
    activeRoomCode = "";
  }

  async function refreshRoom(code) {
    const data = await request(`/api/rooms/${encodeURIComponent(code)}`);
    if (activeRoomCode === code && modal.classList.contains("open")) renderPokerRoom(data);
  }

  async function openRoom(code) {
    stopRoomPolling();
    activeRoomCode = code;
    modal.classList.add("open");
    try {
      await refreshRoom(code);
      roomPoll = setInterval(async () => {
        if (roomBusy || document.activeElement?.closest("#modal-body")) return;
        try {
          await refreshRoom(code);
        } catch (error) {
          showToast(error.message);
          stopRoomPolling();
        }
      }, 2000);
    } catch (error) {
      stopRoomPolling();
      showToast(error.message);
    }
  }

  async function pokerStart() {
    if (!activeRoomCode || roomBusy) return;
    const code = activeRoomCode;
    roomBusy = true;
    try {
      const data = await request(`/api/rooms/${encodeURIComponent(code)}/poker/start`, {});
      if (activeRoomCode === code) renderPokerRoom(data);
    } catch (error) {
      showToast(error.message);
    } finally {
      roomBusy = false;
    }
  }

  async function pokerAction(action, raiseTo) {
    if (!activeRoomCode || roomBusy) return;
    const code = activeRoomCode;
    roomBusy = true;
    try {
      const data = await request(`/api/rooms/${encodeURIComponent(code)}/poker/action`, {
        action,
        ...(raiseTo === undefined ? {} : { raise_to: raiseTo }),
      });
      if (activeRoomCode === code) renderPokerRoom(data);
    } catch (error) {
      showToast(error.message);
      if (activeRoomCode === code) await refreshRoom(code);
    } finally {
      roomBusy = false;
    }
  }

  document.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const gameButton = target.closest("[data-game]");
    const profileButton = target.closest("[data-profile]");
    const toastButton = target.closest("[data-toast]");

    if (gameButton) {
      event.preventDefault();
      event.stopImmediatePropagation();
      bet = 50;
      window.game(gameButton.dataset.game);
      if (gameButton.dataset.game === "blackjack") {
        setResult("Choose your bet, then hit or stand against the dealer.");
        const oldStage = document.querySelector(".stage");
        const table = document.createElement("div");
        table.className = "blackjack-preview-table";
        for (const [label, count] of [["Dealer", 1], ["You", 2]]) {
          const hand = document.createElement("section");
          hand.className = "blackjack-hand";
          const heading = document.createElement("strong");
          heading.textContent = label;
          const cards = document.createElement("div");
          cards.className = "blackjack-cards";
          for (let index = 0; index < count; index += 1) cards.append(blackjackCard(null, true));
          hand.append(heading, cards);
          table.append(hand);
          if (label === "Dealer") {
            const versus = document.createElement("span");
            versus.className = "blackjack-versus";
            versus.textContent = "VS";
            table.append(versus);
          }
        }
        oldStage.replaceWith(table);
      }
      updateBet();
      return;
    }
    if (profileButton) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (profileButton.closest(".bottom")) showProfile();
      else window.profile(profileButton.dataset.profile);
      return;
    }
    if (target.closest("[data-invite]")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      window.invite();
      return;
    }
    if (toastButton) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (toastButton.closest(".bottom")) {
        const label = toastButton.textContent.trim();
        if (label.endsWith("Home")) window.scrollTo({ top: 0, behavior: "smooth" });
        else if (label.endsWith("Games")) document.querySelector(".games").scrollIntoView({ behavior: "smooth" });
        else if (label.endsWith("Rooms")) document.querySelector(".columns").scrollIntoView({ behavior: "smooth" });
        else if (label.endsWith("Profile")) showProfile();
        return;
      }
      showToast(toastButton.dataset.toast);
      return;
    }
    if (target.closest("[data-bet]")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      bet = Math.max(25, Math.min(500, bet + Number(target.closest("[data-bet]").dataset.bet)));
      updateBet();
      return;
    }
    if (target.closest("[data-choice]")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      choice = target.closest("[data-choice]").dataset.choice;
      document.querySelectorAll("[data-choice]").forEach((button) => button.classList.toggle("selected", button.dataset.choice === choice));
      return;
    }
    if (target.closest("#spin")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      play("slots");
      return;
    }
    if (target.closest("#flip-btn")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      play("coin");
      return;
    }
    if (target.closest("#deal")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      blackjackAction("start");
      return;
    }
    if (target.closest("#blackjack-hit")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      blackjackAction("hit");
      return;
    }
    if (target.closest("#blackjack-stand")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      blackjackAction("stand");
      return;
    }
    if (target.closest("#edit-name")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      promptForName(currentUser?.display_name || currentUser?.first_name);
      return;
    }
    if (target.closest("#create")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      createRoom();
      return;
    }
    if (target.closest("#share-room, [data-share-room]")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      share(target.closest("#share-room, [data-share-room]").dataset.url, "Join my Soto game room");
      return;
    }
    if (target.closest("[data-open-room]")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      openRoom(target.closest("[data-open-room]").dataset.openRoom);
      return;
    }
    if (target.closest("#poker-start")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      pokerStart();
      return;
    }
    if (target.closest("#poker-fold, #poker-check, #poker-call")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const action = target.closest("#poker-fold, #poker-check, #poker-call").id.replace("poker-", "");
      pokerAction(action);
      return;
    }
    if (target.closest("#poker-raise")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const raiseTo = Number(document.querySelector("#poker-raise-to").value);
      pokerAction("raise", raiseTo);
      return;
    }
    if (target.closest("#share, #invite-user")) {
      event.preventDefault();
      event.stopImmediatePropagation();
      share(appUrl || window.location.href, "Join me on Soto");
      return;
    }
    if (target.closest(".close") || target === modal) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (modal.dataset.locked === "name") return;
      modal.classList.remove("open");
      stopRoomPolling();
    }
  }, true);

  async function start() {
    setupRoomView();
    telegram?.ready();
    telegram?.expand();
    const telegramUser = telegram?.initDataUnsafe?.user;
    if (telegramUser?.first_name) {
      updateGreeting(telegramUser.first_name);
    }
    if (!initData) {
      showToast("Open Soto from Telegram to sign in.");
      return;
    }
    try {
      const account = await request("/api/me");
      updateBalance(account.balance);
      currentUser = account.user;
      if (!currentUser.display_name) {
        await promptForName(currentUser.first_name, true);
      } else {
        updateGreeting(currentUser.display_name);
      }
      appUrl = account.app_url || "";
      await loadRooms();
      const startParam = telegram?.initDataUnsafe?.start_param || "";
      if (startParam.startsWith("room_")) await joinRoom(startParam.slice(5));
    } catch (error) {
      showToast(error.message);
    }
  }

  start();
})();