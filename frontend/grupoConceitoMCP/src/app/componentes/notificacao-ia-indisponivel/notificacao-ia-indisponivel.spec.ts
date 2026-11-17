import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Sessao } from '../../servicos/sessao/sessao';
import { NotificacaoIaIndisponivel } from './notificacao-ia-indisponivel';

function statusPoller(ia: object | null) {
  return { etapas: [], ultima_rodada_em: null, proxima_rodada_em: null, erro: null, ia };
}

const IA_FORA = {
  indisponivel_desde: '2026-11-17T14:05:00Z',
  modo_regra: false,
  erro: 'APITimeoutError: Request timed out.',
  limite_minutos: 60,
};

describe('NotificacaoIaIndisponivel', () => {
  let http: HttpTestingController;

  function montar(modulos: string[]) {
    TestBed.configureTestingModule({
      imports: [NotificacaoIaIndisponivel],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: Sessao, useValue: { modulos: () => modulos, ehDesenvolvedor: () => false } },
      ],
    });
    http = TestBed.inject(HttpTestingController);
    const fixture = TestBed.createComponent(NotificacaoIaIndisponivel);
    fixture.detectChanges();
    return fixture;
  }

  function responder(ia: object | null) {
    http.expectOne((req) => req.url.endsWith('/api/ti/poller/status')).flush(statusPoller(ia));
  }

  function popup(fixture: ReturnType<typeof montar>, id: string): HTMLElement | null {
    fixture.detectChanges();
    return fixture.nativeElement.querySelector(`[data-testid="${id}"]`);
  }

  afterEach(() => http.verify());

  it('não consulta nada pra quem não tem o módulo TI', () => {
    montar(['financeiro']);
    http.expectNone((req) => req.url.endsWith('/api/ti/poller/status'));
  });

  it('mostra o popup quando a IA está fora do ar, com o erro', () => {
    const fixture = montar(['ti']);
    responder(IA_FORA);

    const elemento = popup(fixture, 'popup-ia-indisponivel');
    expect(elemento).not.toBeNull();
    expect(elemento!.textContent).toContain('IA da triagem fora do ar');
    expect(elemento!.textContent).toContain('adiados');
    expect(elemento!.textContent).toContain('APITimeoutError');
  });

  it('avisa que passou pra regra simples depois do limite', () => {
    const fixture = montar(['ti']);
    responder({ ...IA_FORA, modo_regra: true });

    expect(popup(fixture, 'popup-ia-indisponivel')!.textContent).toContain('regra simples');
  });

  it('não mostra nada com a IA respondendo', () => {
    const fixture = montar(['ti']);
    responder({ indisponivel_desde: null, modo_regra: false, erro: null, limite_minutos: 60 });

    expect(popup(fixture, 'popup-ia-indisponivel')).toBeNull();
  });

  it('fechar esconde o popup', () => {
    const fixture = montar(['ti']);
    responder(IA_FORA);

    popup(fixture, 'popup-ia-indisponivel')!
      .querySelector<HTMLButtonElement>('.toast-fechar')!
      .click();

    expect(popup(fixture, 'popup-ia-indisponivel')).toBeNull();
  });

  it('avisa quando a IA volta', () => {
    const fixture = montar(['ti']);
    responder(IA_FORA);
    popup(fixture, 'popup-ia-indisponivel');

    fixture.componentInstance['poller'].carregar();
    responder({ indisponivel_desde: null, modo_regra: false, erro: null, limite_minutos: 60 });

    expect(popup(fixture, 'popup-ia-voltou')).not.toBeNull();
  });
});
