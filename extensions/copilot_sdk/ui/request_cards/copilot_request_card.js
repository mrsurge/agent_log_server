let bootConfig = { schemas: {} };

function normalizeRequestMethod(value) {
  return typeof value === 'string' ? value.trim().toLowerCase() : '';
}

function normalizeStringList(value) {
  const items = Array.isArray(value) ? value : (typeof value === 'string' ? [value] : []);
  const normalized = [];
  const seen = new Set();
  items.forEach((item) => {
    const text = String(item || '').trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    normalized.push(text);
  });
  return normalized;
}

function appendKeyValue(container, label, value, helpers) {
  if (value === null || value === undefined || value === '') return;
  const row = document.createElement('div');
  row.innerHTML = `<strong>${helpers.escapeHtml(label)}:</strong> ${helpers.escapeHtml(String(value))}`;
  container.append(row);
}

function renderMarkdownNode(container, text, helpers, extraClass = '') {
  if (!(container instanceof HTMLElement)) return;
  if (typeof helpers?.renderMarkdown === 'function') {
    helpers.renderMarkdown(container, text, extraClass);
    return;
  }
  if (typeof extraClass === 'string' && extraClass.trim()) {
    container.className = extraClass.trim();
  }
  container.textContent = String(text || '');
}

function appendMarkdownValue(container, label, value, helpers) {
  if (value === null || value === undefined || value === '') return;
  const row = document.createElement('div');
  const title = document.createElement('div');
  title.innerHTML = `<strong>${helpers.escapeHtml(label)}:</strong>`;
  const content = document.createElement('div');
  renderMarkdownNode(content, String(value), helpers);
  row.append(title, content);
  container.append(row);
}

function createFeedbackNode(body) {
  const feedback = document.createElement('div');
  feedback.className = 'approval-feedback';
  body.append(feedback);
  return feedback;
}

function setFeedback(node, message, isError = false) {
  if (!(node instanceof HTMLElement)) return;
  node.textContent = message || '';
  node.style.color = isError ? '#c62828' : '';
}

function isReadOnlyEvent(event, helpers) {
  return helpers?.readOnly === true
    || event?.replay === true
    || event?.event === 'approval_decision'
    || typeof event?.status === 'string';
}

function readOnlyStatusLabel(event, fallback = 'Recorded response') {
  const parts = [];
  if (typeof event?.status === 'string' && event.status.trim()) parts.push(event.status.trim());
  if (typeof event?.decision === 'string' && event.decision.trim()) parts.push(event.decision.trim());
  if (typeof event?.result?.action === 'string' && event.result.action.trim()) parts.push(event.result.action.trim());
  return parts.length ? `${fallback}: ${parts.join(' / ')}` : fallback;
}

async function trySubmit(helpers, result, meta, feedbackNode, pendingMessage = '') {
  if (pendingMessage) {
    setFeedback(feedbackNode, pendingMessage, false);
  }
  const outcome = await helpers.submitResult(result, meta);
  if (!outcome || outcome.ok === false) {
    const message = outcome?.response?.error || 'Request failed';
    setFeedback(feedbackNode, message, true);
    return false;
  }
  return true;
}

function addJsonDetails(body, label, value) {
  if (value === null || value === undefined || value === '') return;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = label;
  const pre = document.createElement('pre');
  pre.className = 'approval-extra';
  pre.textContent = JSON.stringify(value, null, 2);
  details.append(summary, pre);
  body.append(details);
}

function renderDiffPreview(body, diffText, filePath, helpers) {
  if (!diffText) return;
  const diffBlock = document.createElement('div');
  diffBlock.className = 'diff-block';
  if (typeof helpers.renderDiffBlock === 'function') {
    helpers.renderDiffBlock(diffBlock, diffText, filePath || '');
  } else {
    diffBlock.innerHTML = helpers.formatDiff(diffText, filePath || null);
  }
  body.append(diffBlock);
}

function createSubmittedAnswerNode(answer) {
  const wrapper = document.createElement('div');
  wrapper.className = 'approval-summary';
  const title = document.createElement('div');
  title.innerHTML = '<strong>Response:</strong>';
  wrapper.append(title);
  const value = document.createElement('div');
  value.textContent = String(answer || '');
  wrapper.append(value);
  return wrapper;
}

function decisionLabel(decision, helpers) {
  if (helpers?.normalizeDecisionLabel) {
    return helpers.normalizeDecisionLabel(decision);
  }
  return typeof decision === 'string' ? decision : 'Submit';
}

function responseDecisionOptions(schema, requestParams) {
  const available = Array.isArray(requestParams?.availableDecisions) ? requestParams.availableDecisions : [];
  if (available.length) {
    return available;
  }
  const options = schema?.response?.properties?.decision?.enum;
  return Array.isArray(options) && options.length ? options : ['accept', 'decline'];
}

function appendStringList(body, label, values, helpers, { asLinks = false } = {}) {
  if (!Array.isArray(values) || !values.length) return;
  const wrapper = document.createElement('div');
  wrapper.className = 'approval-summary';
  const title = document.createElement('div');
  title.innerHTML = `<strong>${helpers.escapeHtml(label)}:</strong>`;
  wrapper.append(title);
  values.forEach((value) => {
    if (typeof value !== 'string' || !value.trim()) return;
    const row = document.createElement('div');
    if (asLinks) {
      const link = document.createElement('a');
      link.href = value;
      link.target = '_blank';
      link.rel = 'noreferrer';
      link.textContent = value;
      row.append(link);
    } else {
      row.textContent = value;
    }
    wrapper.append(row);
  });
  body.append(wrapper);
}

function renderPermissionCard(body, event, schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const readOnly = isReadOnlyEvent(event, helpers);
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendKeyValue(summary, 'Kind', requestParams.kind || payload.kind || '', helpers);
  appendKeyValue(summary, 'Tool', payload.tool_name || requestParams.tool_name || '', helpers);
  appendKeyValue(summary, 'Command', Array.isArray(payload.command) ? payload.command.join(' ') : (payload.command || requestParams.command || ''), helpers);
  appendMarkdownValue(summary, 'Intention', requestParams.intention || payload.intention || '', helpers);
  appendKeyValue(summary, 'Path', payload.path || requestParams.path || '', helpers);
  appendKeyValue(summary, 'CWD', payload.cwd || requestParams.cwd || '', helpers);
  body.append(summary);

  const warning = requestParams.warning || payload.warning;
  if (typeof warning === 'string' && warning.trim()) {
    const warningNode = document.createElement('div');
    renderMarkdownNode(warningNode, warning, helpers, 'approval-feedback');
    body.append(warningNode);
  }

  const diffText = payload.diff || requestParams.diff || '';
  const filePath = payload.path || requestParams.path || '';
  if (diffText) {
    renderDiffPreview(body, diffText, filePath, helpers);
  }

  appendStringList(body, 'Possible Paths', requestParams.possible_paths || payload.possible_paths, helpers);
  appendStringList(body, 'Possible URLs', requestParams.possible_urls || payload.possible_urls, helpers, { asLinks: true });
  addJsonDetails(body, 'Tool arguments', payload.arguments ?? requestParams.arguments ?? null);
  addJsonDetails(body, 'Request details', requestParams.request ?? payload.request ?? null);
  addJsonDetails(body, 'Change preview', payload.changes ?? requestParams.changes ?? null);

  const feedback = createFeedbackNode(body);
  if (readOnly) {
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }

  const actions = document.createElement('div');
  actions.className = 'actions';
  responseDecisionOptions(schema, requestParams).forEach((decision) => {
    const button = document.createElement('button');
    button.className = decision === 'decline' || decision === 'cancel' ? 'btn tiny decline' : 'btn tiny approve';
    button.textContent = decisionLabel(decision, helpers);
    button.addEventListener('click', async () => {
      await trySubmit(helpers, { decision }, { diff: diffText, path: filePath }, feedback, 'Sending response…');
    });
    actions.append(button);
  });
  body.append(actions);
}

function renderUserInputCard(body, event, _schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const question = String(requestParams.question || payload.question || '').trim();
  const choices = Array.isArray(requestParams.choices) ? requestParams.choices : (Array.isArray(payload.choices) ? payload.choices : []);
  const allowFreeform = requestParams.allowFreeform !== undefined
    ? requestParams.allowFreeform !== false
    : payload.allowFreeform !== false;
  const readOnly = isReadOnlyEvent(event, helpers);
  const result = event?.result && typeof event.result === 'object' ? event.result : {};
  body.innerHTML = '';

  const summary = document.createElement('div');
  summary.className = 'approval-summary';
  appendMarkdownValue(summary, 'Question', question, helpers);
  body.append(summary);

  const recordedAnswer = typeof result.answer === 'string' ? result.answer : '';

  const rerenderSubmitted = (answer, wasFreeform) => {
    renderUserInputCard(
      body,
      {
        ...event,
        status: 'submitted',
        result: { answer, wasFreeform },
      },
      _schema,
      {
        ...helpers,
        readOnly: true,
      },
    );
  };

  if (choices.length) {
    const optionsWrap = document.createElement('div');
    optionsWrap.className = 'approval-option-list';
    choices.forEach((choice) => {
      if (typeof choice !== 'string' || !choice.trim()) return;
      const item = document.createElement('div');
      item.className = 'approval-option-item';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn tiny';
      button.textContent = choice;
      if (recordedAnswer === choice) {
        button.classList.add('approve');
      }
      if (readOnly) {
        button.disabled = true;
      } else {
        button.addEventListener('click', async () => {
          const ok = await trySubmit(helpers, { answer: choice, wasFreeform: false }, {}, feedback, 'Sending response…');
          if (ok) {
            rerenderSubmitted(choice, false);
          }
        });
      }
      item.append(button);
      optionsWrap.append(item);
    });
    body.append(optionsWrap);
  }

  const feedback = createFeedbackNode(body);
  if (readOnly) {
    feedback.classList.add('approval-feedback-static');
    setFeedback(feedback, readOnlyStatusLabel(event), false);
    if (recordedAnswer) {
      body.insertBefore(createSubmittedAnswerNode(recordedAnswer), feedback);
    }
    if (event?.result && typeof event.result === 'object') {
      addJsonDetails(body, 'Recorded result', event.result);
    }
    return;
  }

  let input = null;
  if (allowFreeform) {
    const freeformLabel = document.createElement('div');
    freeformLabel.innerHTML = '<strong>Something else:</strong>';
    body.append(freeformLabel);

    input = document.createElement('textarea');
    input.className = 'input approval-answer-field';
    input.rows = 3;
    input.placeholder = 'Enter response';
    if (recordedAnswer) {
      input.value = recordedAnswer;
    }
    input.addEventListener('input', () => {
      input.dataset.answerSource = 'freeform';
    });
    body.append(input);
  }

  const actions = document.createElement('div');
  actions.className = 'actions';
  if (allowFreeform && input) {
    const sendButton = document.createElement('button');
    sendButton.className = 'btn tiny approve';
    sendButton.textContent = 'Send';
    sendButton.addEventListener('click', async () => {
      const answer = String(input.value || '').trim();
      if (!answer) {
        setFeedback(feedback, 'Enter or select a response first.', true);
        return;
      }
      const wasFreeform = input.dataset.answerSource === 'freeform' || !choices.includes(answer);
      const ok = await trySubmit(helpers, { answer, wasFreeform }, {}, feedback, 'Sending response…');
      if (ok) {
        rerenderSubmitted(answer, wasFreeform);
      }
    });
    actions.append(sendButton);
  }
  body.append(actions);
}

function renderAgentPtyAskUserCard(body, event, _schema, helpers) {
  const requestParams = event.request_params && typeof event.request_params === 'object' ? event.request_params : {};
  const payload = event.payload && typeof event.payload === 'object' ? event.payload : {};
  const question = String(requestParams.question || payload.question || payload.message || '').trim();
  const choices = normalizeStringList(requestParams.choices ?? payload.choices);
  const allowFreeform = requestParams.allowFreeform !== undefined
    ? requestParams.allowFreeform !== false
    : payload.allowFreeform !== false;
  const syntheticEvent = {
    ...event,
    status: event?.status || (typeof event?.result?.action === 'string' ? event.result.action : undefined),
    request_params: {
      ...requestParams,
      question,
      choices,
      allowFreeform,
    },
    payload: {
      ...payload,
      question,
      choices,
      allowFreeform,
    },
    result: {
      answer: typeof event?.result?.answer === 'string' ? event.result.answer : '',
      wasFreeform: event?.result?.wasFreeform === true
        || (typeof event?.result?.freeform_answer === 'string' && event.result.freeform_answer.trim().length > 0),
    },
  };
  const syntheticHelpers = {
    ...helpers,
    submitResult: async (result, meta) => {
      const answer = typeof result?.answer === 'string' ? result.answer.trim() : '';
      const answers = answer ? [answer] : [];
      const selectedChoice = answer && choices.includes(answer) ? answer : null;
      const freeformAnswer = answer && !selectedChoice ? answer : null;
      return helpers.submitResult({
        action: 'accept',
        answer: answer || null,
        answers,
        selected_choice: selectedChoice,
        freeform_answer: freeformAnswer,
        wasFreeform: Boolean(result?.wasFreeform) || Boolean(freeformAnswer),
      }, meta);
    },
  };
  renderUserInputCard(body, syntheticEvent, _schema, syntheticHelpers);
  if (isReadOnlyEvent(event, helpers)) {
    return;
  }
  const feedback = body.querySelector('.approval-feedback');
  const feedbackNode = feedback instanceof HTMLElement ? feedback : createFeedbackNode(body);
  let actions = body.querySelector('.actions');
  if (!(actions instanceof HTMLElement)) {
    actions = document.createElement('div');
    actions.className = 'actions';
    body.append(actions);
  }
  const declineButton = document.createElement('button');
  declineButton.className = 'btn tiny decline';
  declineButton.textContent = 'Decline';
  declineButton.addEventListener('click', async () => {
    await trySubmit(helpers, { action: 'decline' }, {}, feedbackNode, 'Sending response…');
  });
  actions.append(declineButton);

  const cancelButton = document.createElement('button');
  cancelButton.className = 'btn tiny decline';
  cancelButton.textContent = 'Cancel';
  cancelButton.addEventListener('click', async () => {
    await trySubmit(helpers, { action: 'cancel' }, {}, feedbackNode, 'Sending response…');
  });
  actions.append(cancelButton);
}

export function initializeRequestCardModule(config = {}) {
  bootConfig = {
    schemas: config.schemas && typeof config.schemas === 'object' ? config.schemas : {},
  };
}

export async function renderRequestCard(ctx = {}) {
  const event = ctx.event && typeof ctx.event === 'object' ? ctx.event : {};
  const helpers = ctx.helpers && typeof ctx.helpers === 'object' ? ctx.helpers : {};
  const body = ctx.body;
  if (!(body instanceof HTMLElement)) return false;
  const requestMethod = normalizeRequestMethod(event.request_method || event.requestMethod);
  const schema = ctx.schema || bootConfig.schemas?.[requestMethod] || null;

  if (requestMethod === 'copilot/permission/request') {
    renderPermissionCard(body, event, schema, helpers);
    return true;
  }
  if (requestMethod === 'agent-pty/ask-user') {
    renderAgentPtyAskUserCard(body, event, schema, helpers);
    return true;
  }
  if (requestMethod === 'copilot/user_input/request') {
    renderUserInputCard(body, event, schema, helpers);
    return true;
  }
  return false;
}
