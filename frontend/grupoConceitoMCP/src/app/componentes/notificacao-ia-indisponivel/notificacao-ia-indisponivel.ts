import { Component, DestroyRef, computed, effect, inject, signal } from '@angular/core';
import { PollerTi } from '../../servicos/poller-ti/poller-ti';
import { Sessao } from '../../servicos/sessao/sessao';

/** Intervalo de consulta do status do poller. A rodada do poller é de 5
 * min, então 1 min é suficiente pra avisar logo sem pesar no backend. */
export const INTERVALO_CONSULTA_MS = 60_000;
const DURACAO_AVISO_RETORNO_MS = 8000;

/** Popup global (canto superior direito, abaixo do sino) avisando que a IA
 * da triagem de chamados está fora do ar — antes isso só aparecia no log do
 * poller. Só pra quem tem o módulo TI (a rota de status exige isso). Fica
 * na tela até alguém fechar; reaparece se o estado mudar (ex: passou de
 * "adiando chamados" pra "decidindo pela regra simples"). Quando a IA
 * volta, mostra um aviso curto de retorno que some sozinho. */
@Component({
  selector: 'app-notificacao-ia-indisponivel',
  imports: [],
  templateUrl: './notificacao-ia-indisponivel.html',
  styleUrl: './notificacao-ia-indisponivel.scss',
})
export class NotificacaoIaIndisponivel {
  private readonly poller = inject(PollerTi);
  private readonly sessao = inject(Sessao);

  private readonly chaveDispensada = signal<string | null>(null);
  protected readonly mostrarRetorno = signal(false);
  private estavaIndisponivel = false;

  protected readonly ia = this.poller.ia;

  /** Identifica a "situação" atual: mudar de adiando → regra gera uma
   * chave nova e o popup volta mesmo se o anterior foi fechado. */
  private readonly chaveAtual = computed(() => {
    const ia = this.ia();
    return ia?.indisponivel_desde ? `${ia.indisponivel_desde}|${ia.modo_regra}` : null;
  });

  protected readonly visivel = computed(() => {
    const chave = this.chaveAtual();
    return chave !== null && chave !== this.chaveDispensada();
  });

  protected readonly desdeFormatado = computed(() => {
    const desde = this.ia()?.indisponivel_desde;
    return desde
      ? new Date(desde).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' })
      : '';
  });

  constructor() {
    const temModuloTi = this.sessao.modulos().includes('ti') || this.sessao.ehDesenvolvedor();
    if (!temModuloTi) {
      return;
    }

    this.poller.carregar();
    const intervalo = setInterval(() => this.poller.carregar(), INTERVALO_CONSULTA_MS);
    inject(DestroyRef).onDestroy(() => clearInterval(intervalo));

    effect(() => {
      const indisponivel = this.chaveAtual() !== null;
      if (this.estavaIndisponivel && !indisponivel) {
        this.mostrarRetorno.set(true);
        setTimeout(() => this.mostrarRetorno.set(false), DURACAO_AVISO_RETORNO_MS);
      }
      this.estavaIndisponivel = indisponivel;
    });
  }

  protected dispensar(): void {
    this.chaveDispensada.set(this.chaveAtual());
  }

  protected fecharRetorno(): void {
    this.mostrarRetorno.set(false);
  }
}
